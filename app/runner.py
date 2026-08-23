"""Executes a run inside an ephemeral Docker container and streams its events.

For each run we launch `docker run --rm -i <task-image>`, write the task spec
to the container's stdin as JSON, and read JSONL events from its stdout. Each
event is persisted (db.add_event) and fanned out to any live SSE subscribers.
Concurrency is bounded by a semaphore; each run has a wall-clock timeout after
which the container is killed.
"""
import asyncio
import json
import os
import shlex
from typing import Optional
from urllib.parse import unquote, urlparse

from . import alerts, config, connectors, db, providers, router

# bounded concurrency across all runs
_sem: Optional[asyncio.Semaphore] = None

# run_id -> set of asyncio.Queue for live SSE subscribers
_subscribers: dict[int, set[asyncio.Queue]] = {}


def init() -> None:
    global _sem
    _sem = asyncio.Semaphore(config.MAX_CONCURRENT)


def subscribe(run_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(run_id, set()).add(q)
    return q


def unsubscribe(run_id: int, q: asyncio.Queue) -> None:
    subs = _subscribers.get(run_id)
    if subs:
        subs.discard(q)
        if not subs:
            _subscribers.pop(run_id, None)


def _publish(run_id: int, event: dict) -> None:
    for q in list(_subscribers.get(run_id, ())):
        q.put_nowait(event)


def agent_volume(agent_id: int) -> str:
    """Name of the per-agent persistent workspace volume (the isolation boundary)."""
    return f"superagent-agent-{agent_id}-ws"


async def test_db_connection(engine: str, dsn: str, mode: str) -> dict:
    """Check a data source is reachable, in the SAME task container the agent uses
    (so a pass means the agent will connect too). Runs a trivial query with the
    engine's own client; secrets travel via env (value-less -e), never argv.
    Returns {"ok": bool, "message": str}."""
    from .connectors import db_driver
    eff = db_driver.effective_dsn(engine, dsn, mode)
    inject = {"SADSN": eff}
    if engine == "postgres":
        inject["PGCONNECT_TIMEOUT"] = "8"
        inner = 'psql "$SADSN" -tAc "SELECT 1"'
    elif engine == "mongo":
        inner = ("python3 -c \"import os,pymongo;"
                 "pymongo.MongoClient(os.environ['SADSN'],serverSelectionTimeoutMS=6000)"
                 ".admin.command('ping');print('ok')\"")
    elif engine == "mysql":
        u = urlparse(dsn)
        host = u.hostname or "localhost"
        port = u.port or 3306
        user = unquote(u.username or "")
        inject["MYSQL_PWD"] = unquote(u.password or "")
        dbn = (u.path or "").lstrip("/")
        dbflag = f"-D {shlex.quote(dbn)}" if dbn else ""
        inner = (f"mysql --connect-timeout=8 -h {shlex.quote(host)} -P {port} "
                 f"-u {shlex.quote(user)} {dbflag} -e 'SELECT 1'")
    else:
        return {"ok": False, "message": f"unsupported engine {engine}"}

    # override the image's run_task.py entrypoint so we run a bare shell instead
    cmd = ["docker", "run", "--rm", "--network", config.TASK_NETWORK,
           "--memory", "256m", "--pids-limit", "64", "--entrypoint", "sh"]
    for key in inject:                       # value-less -e pulls from full_env below
        cmd += ["-e", key]
    cmd += [config.TASK_IMAGE, "-c", inner]
    full_env = {**os.environ, **inject}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=full_env)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "timed out (30s) connecting to the database"}
    except FileNotFoundError:
        return {"ok": False, "message": "docker not available on the host"}
    if proc.returncode == 0:
        return {"ok": True, "message": "connected"}
    detail = (err.decode(errors="replace").strip()
              or out.decode(errors="replace").strip() or f"exit {proc.returncode}")
    return {"ok": False, "message": detail[-400:]}


# Reading an agent's artifacts goes THROUGH the Docker daemon — we mount the
# workspace volume read-only into a short-lived helper container and read from
# there — rather than off a host path. This works identically on Linux, macOS,
# and Windows (where the daemon, and thus the volume storage, lives in a VM and
# is not on the host filesystem). It reads existing volumes in place: no data is
# moved or copied. Mirrors the `--entrypoint` helper pattern in
# test_db_connection; the helper reuses the always-present task image and runs
# with no network.

# find under /ws/artifacts: prune scratch dirs, skip dot paths, print
# "<relpath>\t<size>\t<mtime-epoch>" per file.
_ARTIFACT_LIST_SCRIPT = (
    "cd /ws/artifacts 2>/dev/null || exit 0; "
    "find . -name .git -prune -o -name node_modules -prune -o "
    "-name __pycache__ -prune -o -name .cache -prune -o -name .npm -prune -o "
    '-type f ! -path "*/.*" -printf "%P\\t%s\\t%T@\\n"'
)


async def _volume_exists(agent_id: int) -> bool:
    """True if the agent's workspace volume exists (i.e. it has ever run). Uses a
    daemon metadata query, so it works on every platform (no host path)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "volume", "inspect", agent_volume(agent_id),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception:  # noqa: BLE001
        return False


async def list_artifacts(agent_id: int) -> Optional[list[dict]]:
    """List files under the agent's <workspace>/artifacts, read via a throwaway
    container. Returns [{"path", "size", "mtime"}] (unsorted), [] if the agent has
    a volume but no artifacts, or None if it has no workspace volume yet."""
    if not await _volume_exists(agent_id):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{agent_volume(agent_id)}:/ws:ro",
            "--entrypoint", "sh", config.TASK_IMAGE, "-c", _ARTIFACT_LIST_SCRIPT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception:  # noqa: BLE001
        return []
    rows: list[dict] = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        path, size, mtime = parts
        try:
            rows.append({"path": path, "size": int(size), "mtime": float(mtime)})
        except ValueError:
            continue
    return rows


async def read_artifact(agent_id: int, ws_path: str) -> Optional[bytes]:
    """Read one file from the agent's workspace volume via a throwaway container.
    `ws_path` is an absolute path under /ws (already traversal-validated by the
    caller). Returns the file bytes, or None if the volume/file doesn't exist.
    Reads the file fully into memory — fine for deliverables."""
    if not await _volume_exists(agent_id):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{agent_volume(agent_id)}:/ws:ro",
            "--entrypoint", "sh", config.TASK_IMAGE,
            "-c", '[ -f "$1" ] && cat -- "$1" || exit 44', "sa", ws_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:   # 44 = not a file / missing
        return None
    return out


async def write_upload(agent_id: int, filename: str, data: bytes) -> str:
    """Write a user-uploaded file into the agent's workspace under uploads/, via a
    throwaway helper container (mirror of read_artifact, mounted :rw). `filename`
    must already be sanitized to a safe basename by the caller. Returns the
    workspace-relative path ("uploads/<filename>"). Raises RuntimeError on failure.

    The helper runs as the image's `agent` user — the same uid that owns
    /workspace — so mkdir/write succeed; the volume is auto-created by the mount if
    the agent has never run. Data is streamed to the helper's stdin, not held in
    argv."""
    # Mount at /workspace (not /ws): the image prepares /workspace owned by the
    # `agent` user, so on a brand-new volume the helper (running as agent) can
    # mkdir/write. A bare /ws would be created root-owned and the write would fail.
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "--rm", "-i", "--network", "none",
        "-v", f"{agent_volume(agent_id)}:/workspace:rw",
        "--entrypoint", "sh", config.TASK_IMAGE,
        "-c", 'mkdir -p /workspace/uploads && cat > "/workspace/uploads/$1"', "sa", filename,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(input=data), timeout=120)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"upload failed: {e}")
    if proc.returncode != 0:
        raise RuntimeError(f"upload failed: {err.decode('utf-8', 'replace')[:200]}")
    return f"uploads/{filename}"


# Injected into every run's system prompt so deliverables land somewhere the UI can
# surface, separate from the agent's scratch (scripts, logs, intermediate downloads).
_ARTIFACTS_NOTE = (
    "ARTIFACTS / DELIVERABLES: When the user asks you to produce something they will want to "
    "keep, view, or download — a document, report, HTML page, chart, image, CSV/spreadsheet, "
    "slides, etc. — save the FINISHED file(s) into the ./artifacts/ directory (run "
    "`mkdir -p artifacts` first). Put ONLY finished deliverables there; keep scratch work, "
    "helper scripts, logs, and intermediate downloads OUT of ./artifacts/ (use /tmp or the "
    "working directory for those). Make HTML deliverables self-contained with inline CSS so "
    "they render correctly on their own."
)

# Injected into chat runs so the agent doesn't try (and fail) to set up cron or
# background jobs inside its ephemeral container — and instead points the user at
# the one-click "Save as routine" flow.
_SCHEDULING_NOTE = (
    "SCHEDULING / RECURRING WORK: You run in a fresh, ephemeral container that is "
    "destroyed when this turn ends — you CANNOT set up cron jobs, background daemons, "
    "or anything that persists after you finish (there is no cron, and you are not "
    "root). If the user wants something to run on a recurring schedule, do NOT attempt "
    "cron/systemd and do NOT claim you scheduled it. Instead, do the task now, then "
    "tell them they can make it recurring in one click with the 'Save as routine' "
    "button on this chat — and propose a concrete schedule (e.g. \"every day at 9am\")."
)

# Injected into chat runs so the agent knows where user-uploaded files land and how
# to read each type (documents need anydoc; tabular data uses pandas).
_UPLOADS_NOTE = (
    "UPLOADED FILES: Files the user attaches are saved in the /workspace/uploads/ "
    "directory (your working directory is /workspace, so ./uploads/). Always use "
    "that exact path — do NOT cd elsewhere (e.g. /tmp) or invent another location. "
    "Look there when the user refers to an attachment.\n"
    "- DOCUMENTS (PDF, Word .docx, PowerPoint .pptx, Excel .xlsx, OpenDocument, RTF, "
    "EPUB): convert to Markdown with the `anydoc` CLI — do NOT hand-roll a parser. "
    "IMPORTANT: write the output to a FILE, never dump a whole document into your "
    "context (it can exceed the model's limit and fail the run). Do "
    "`anydoc ./uploads/report.pdf > ./uploads/report.md`, then work with the file "
    "incrementally: check size first (`wc -l`/`wc -c`), read the top with `head`, "
    "`grep` for what you need, and for a long document summarize it section by "
    "section rather than reading it all at once.\n"
    "- CSV / tabular data: use pandas (read, aggregate, filter) — don't print huge "
    "tables into context.\n"
    "- Plain text / code: read directly, but for large files read in ranges rather "
    "than the whole thing."
)


def _build_spec(agent: dict, model: str, *, prompt: str, resume: Optional[str],
                mcp_servers: Optional[dict], skills: Optional[list],
                db_note: Optional[str] = None, search_note: Optional[str] = None) -> dict:
    system_prompt = agent.get("system_prompt") or ""
    notes = [db_note, search_note, _ARTIFACTS_NOTE]
    # Uploads work for chats and (interactive) routines alike — inject the note for
    # both. It's inert on a scheduled routine run (no files uploaded then).
    notes.append(_UPLOADS_NOTE)
    if agent.get("kind") == "chat":
        notes.append(_SCHEDULING_NOTE)
    for note in notes:
        if note:
            system_prompt = (system_prompt + "\n\n" + note).strip()
    spec = {
        "prompt": prompt,
        "model": model,
        "system_prompt": system_prompt or None,
        "allowed_tools": agent.get("allowed_tools"),
        "max_turns": agent.get("max_turns") or config.DEFAULT_MAX_TURNS,
        "permission_mode": "bypassPermissions",
        "cwd": "/workspace",
        # a chat/follow-up run resumes the agent's prior conversation session
        "resume": resume,
        # connector tools for the agent owner (e.g. Composio per-user MCP server)
        "mcp_servers": mcp_servers,
        # packaged skills to materialize into the container's skills dir
        "skills": skills or None,
    }
    return {k: v for k, v in spec.items() if v is not None}


def _docker_cmd(run_id: int, agent_id: int, inject_env: dict) -> list[str]:
    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", f"superagent-run-{run_id}",
        "--network", config.TASK_NETWORK,
        "--memory", config.TASK_MEMORY,
        "--cpus", config.TASK_CPUS,
        "--pids-limit", config.TASK_PIDS,
        # per-agent persistent workspace: survives across runs so chat/follow-ups
        # keep files AND the session transcript (under CLAUDE_CONFIG_DIR below).
        # This named volume is only ever mounted for this agent's containers.
        "-v", f"{agent_volume(agent_id)}:/workspace",
        "-e", "CLAUDE_CONFIG_DIR=/workspace/.claude",
    ]
    # value-less -e pulls each var from the docker client's env (set below),
    # keeping secrets out of the process argument list.
    for key in inject_env:
        cmd += ["-e", key]
    cmd += [config.TASK_IMAGE]
    return cmd


async def execute_run(run_id: int, agent: dict) -> None:
    """Run one container for a run of `agent` to completion. Never raises."""
    assert _sem is not None, "runner.init() not called"
    seq = 0
    result_text: Optional[str] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None

    # the run row carries this execution's prompt + whether to resume the session
    run = await db.get_run(run_id)
    prompt = (run and run.get("prompt")) or agent.get("prompt") or ""
    resume = agent.get("last_session_id") if (run and run.get("resume")) else None
    mcp_servers = None

    async def handle(event: dict) -> None:
        nonlocal seq, result_text, cost_usd, error
        seq += 1
        etype = event.get("type", "event")
        await db.add_event(run_id, seq, etype, event)
        _publish(run_id, {"seq": seq, **event})
        if etype == "result":
            cost_usd = event.get("cost_usd")
            # An error result (e.g. "Prompt is too long") carries the REAL reason in
            # `result`; capture it as the run error so the user sees that instead of
            # the SDK's cryptic "returned an error result: success" exception that
            # follows. A normal result populates result_text.
            if event.get("is_error"):
                error = event.get("result") or error or "the model returned an error"
            else:
                result_text = event.get("result")
            # persist the session id so a chat/follow-up run can resume it
            sid = event.get("session_id")
            if sid:
                await db.set_run_session_id(run_id, sid)
                await db.set_agent_session(agent["id"], sid)
        elif etype == "error":
            # don't let the SDK's generic wrapper overwrite a specific reason we
            # already captured from the error result above.
            if not error:
                error = event.get("message")

    async with _sem:
        await db.mark_run_started(run_id)

        # pick provider + model for this agent, and resolve the provider's env
        route_input = {"provider": agent.get("provider"), "model": agent.get("model"),
                       "prompt": prompt}
        try:
            provider_name, model = router.route(route_input)
            provider_env = providers.container_env(provider_name, model)
        except ValueError as e:
            await handle({"type": "error", "message": str(e)})
            await db.finish_run(run_id, status="failed", error=str(e))
            _publish(run_id, {"type": "status", "status": "failed"})
            _publish(run_id, None)
            return

        await handle({"type": "routed", "provider": provider_name, "model": model})

        # provision ONLY the connectors this agent selected, intersected with the
        # owner's currently-active connections. Best-effort: a connector hiccup
        # degrades to "no tools", never fails the run.
        owner_id = agent.get("owner_id")
        is_chat = agent.get("kind") == "chat"
        selected = await db.get_agent_connectors(agent["id"])
        # a routine uses only the connectors it selected; a chat thread can use
        # everything the owner has connected.
        if owner_id is not None and (selected or is_chat):
            try:
                active = await db.active_providers(owner_id)
                use = list(active) if is_chat else [p for p in active if p in selected]
                if use:
                    prov = await asyncio.to_thread(
                        connectors.get_connector().agent_provisioning, owner_id, use
                    )
                    if prov and prov.get("mcp_servers"):
                        mcp_servers = prov["mcp_servers"]
                        await handle({"type": "tools", "connected": use})
            except Exception as e:  # noqa: BLE001
                await handle({"type": "log", "text": f"connector provisioning skipped: {e}"})

        # the agent's attached skills, materialized into the container by run_task.py
        skills = [{"name": s["name"], "description": s.get("description") or "",
                   "instructions": s.get("instructions") or ""}
                  for s in await db.get_agent_skills(agent["id"])]
        # always make the built-in Artifact Design skill available (loaded on demand
        # by the SDK, so it only engages when the agent produces HTML). Skip if the
        # agent already has a same-named skill attached, so edits/overrides win.
        if not any(s["name"] == db.ARTIFACT_DESIGN_SKILL["name"] for s in skills):
            design = await db.get_skill_by_name(agent.get("org_id"), db.ARTIFACT_DESIGN_SKILL["name"])
            src = design or db.ARTIFACT_DESIGN_SKILL
            skills.append({"name": src["name"],
                           "description": src.get("description") or "",
                           "instructions": src.get("instructions") or ""})

        # decrypt the agent's DB connections, inject each as an env var, note them
        db_env: dict = {}
        db_lines: list[str] = []
        # data sources are a shared team asset: a chat can query every data source
        # in its org; a routine only the ones explicitly attached to it. (Connectors,
        # by contrast, stay personal — see the owner-scoped provisioning above.)
        if is_chat and agent.get("org_id") is not None:
            db_conns = await db.list_org_db_connections(agent["org_id"])
        else:
            db_conns = await db.get_agent_db_connections(agent["id"])
        for c in db_conns:
            try:
                from .connectors import db_driver
                var = db_driver.env_var(c["name"])
                db_env[var] = db_driver.effective_dsn(
                    c["engine"], db_driver.decrypt(c["dsn_encrypted"]), c["mode"])
                db_lines.append(
                    f"- {c['name']} ({c['engine']}, {c['mode']}) — connection string in "
                    f"${var}; query with {db_driver.client_hint(c['engine'])}")
            except Exception as e:  # noqa: BLE001
                await handle({"type": "log", "text": f"db connection {c.get('name')} skipped: {e}"})
        db_note = None
        if db_lines:
            db_note = ("Available database connections (do NOT write to read_only ones):\n"
                       + "\n".join(db_lines))

        _publish(run_id, {"seq": seq, "type": "status", "status": "running"})

        # expose a search API key to the `websearch` command if the operator set one
        # (it falls back to free DuckDuckGo when neither is present)
        search_keys = {k: os.environ[k] for k in ("TAVILY_API_KEY", "BRAVE_API_KEY")
                       if os.environ.get(k)}
        # WebSearch is an Anthropic server-side tool; on other providers it returns
        # nothing, so steer the agent to the provider-agnostic `websearch` command.
        search_note = None
        if provider_name != "anthropic":
            search_note = (
                "WEB SEARCH: the built-in WebSearch tool does not work with this model "
                "and returns no results. To search the web, run the shell command:\n"
                '  websearch "your query"\n'
                "It prints JSON results (title, url, snippet). Then use WebFetch on the "
                "specific URLs you need. Do not call the WebSearch tool."
            )

        # docker-client env: our own env + provider secrets + per-agent access,
        # so value-less `-e KEY` picks each up here (not from argv).
        inject_env = {**provider_env, **db_env, **search_keys, **(agent.get("env") or {})}
        full_env = {**os.environ, **inject_env}

        try:
            proc = await asyncio.create_subprocess_exec(
                *_docker_cmd(run_id, agent["id"], inject_env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=full_env,
                limit=config.RUN_STREAM_LIMIT,  # one JSON event per line can be large
            )
        except FileNotFoundError:
            await handle({"type": "error", "message": "docker not found on PATH"})
            await db.finish_run(run_id, status="failed", error="docker not found")
            _publish(run_id, {"type": "status", "status": "failed"})
            _publish(run_id, None)  # sentinel: close SSE
            return

        # feed the spec, then close stdin so the container can start
        proc.stdin.write((json.dumps(_build_spec(
            agent, model, prompt=prompt, resume=resume,
            mcp_servers=mcp_servers, skills=skills, db_note=db_note,
            search_note=search_note)) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def pump_stdout() -> None:
            assert proc.stdout is not None
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError:
                    # a single line blew past RUN_STREAM_LIMIT; readline clears the
                    # oversized data from the buffer, so we skip that one event and
                    # keep streaming instead of failing the whole run
                    await handle({"type": "log",
                                  "text": "[event dropped: exceeded stream buffer limit]"})
                    continue
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "log", "text": line}
                await handle(event)

        try:
            await asyncio.wait_for(pump_stdout(), timeout=config.TASK_TIMEOUT)
            await proc.wait()
            exit_code = proc.returncode
        except asyncio.TimeoutError:
            error = f"timed out after {config.TASK_TIMEOUT}s"
            await handle({"type": "error", "message": error})
            await _kill_container(run_id, proc)
            exit_code = 124
        except Exception as e:  # noqa: BLE001 - never let a run crash the server
            error = f"runner error: {type(e).__name__}: {e}"
            await handle({"type": "error", "message": error})
            await _kill_container(run_id, proc)
            exit_code = 1

        if error is None and exit_code not in (0, None):
            stderr = b""
            if proc.stderr is not None:
                try:
                    stderr = await proc.stderr.read()
                except Exception:
                    pass
            error = stderr.decode(errors="replace").strip()[-2000:] or f"exit {exit_code}"

        status = "succeeded" if (exit_code == 0 and not error) else "failed"
        await db.finish_run(
            run_id, status=status, exit_code=exit_code,
            cost_usd=cost_usd, result=result_text, error=error,
        )
        _publish(run_id, {"type": "status", "status": status,
                          "exit_code": exit_code, "cost_usd": cost_usd})
        _publish(run_id, None)  # sentinel: tell SSE streams to close

        # fire any matching alert rules; never let alerting fail the run
        try:
            await alerts.evaluate_run(run_id)
        except Exception:
            pass


async def _kill_container(run_id: int, proc) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "docker", "kill", f"superagent-run-{run_id}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


async def remove_agent_volume(agent_id: int) -> None:
    """Best-effort delete of an agent's persistent workspace volume (on delete).

    Retries briefly: a just-finished container may still hold the volume for a
    moment after `docker run --rm` returns.
    """
    for _ in range(5):
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "volume", "rm", agent_volume(agent_id),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 or b"No such volume" in stderr:
                return
        except Exception:
            return
        await asyncio.sleep(0.5)


async def start_run(run_id: int, agent: dict) -> None:
    """Fire-and-forget a run as a background task."""
    asyncio.create_task(execute_run(run_id, agent))

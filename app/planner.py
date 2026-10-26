"""Job planner — step 4 of docs/features/outcome-jobs.md.

Turns a plain-language description of an outcome into a draft job spec whose
checks can actually run. The model only sees grounded, read-only context: the
routine's instructions, what recent runs reported, the files its workspace really
contains, the schema of the workspace's data sources, and which delivery
channels are connected. Every proposed check goes through the same validator as
a hand-written spec; anything that fails is dropped with a visible warning.
"""
import json
import logging
import re
from typing import Optional

from . import config, db, deliver, llm, verify
from .connectors import db_driver
from .db_access import _PLATFORM_SCHEMAS

log = logging.getLogger("antonagents.planner")

_SCHEMA_SQL = """
SELECT c.table_schema || '.' || c.table_name,
       string_agg(c.column_name || ' ' || c.data_type, ', ' ORDER BY c.ordinal_position)
FROM information_schema.columns c
WHERE c.table_schema NOT IN ('pg_catalog','information_schema')
  AND c.table_schema NOT LIKE 'pg\\_%%'
  AND c.table_schema NOT IN (%s)
GROUP BY 1 ORDER BY 1 LIMIT 60
""".strip()

_FILES_SH = ("cd /ws 2>/dev/null && find . -maxdepth 3 -type f "
             "-not -path './.claude/*' -not -path '*/node_modules/*' -not -path './.cache/*' "
             "-printf '%P\\t%s\\n' 2>/dev/null | head -150")


async def _schema(conn: dict) -> Optional[str]:
    """Tables + columns of a Postgres data source (read-only, capped)."""
    if conn["engine"] != "postgres":
        return None
    platform = ",".join(f"'{s}'" for s in sorted(_PLATFORM_SCHEMAS))
    dsn = db_driver.effective_dsn("postgres", db_driver.decrypt(conn["dsn_encrypted"]), "read_only")
    inner = ('psql "$SADSN" -qtA -1 -v ON_ERROR_STOP=1 -c "SET TRANSACTION READ ONLY" '
             '-c "SET LOCAL statement_timeout=20000" -c "$SAQUERY"')
    inject = {"SADSN": dsn, "SAQUERY": _SCHEMA_SQL % platform, "PGCONNECT_TIMEOUT": "8"}
    cmd = ["docker", "run", "--rm", "--network", config.TASK_NETWORK, "--memory", "256m",
           "--pids-limit", "64", "--entrypoint", "sh"]
    for k in inject:
        cmd += ["-e", k]
    cmd += [config.TASK_IMAGE, "-c", inner]
    code, out, _ = await verify.run_container(cmd, inject, timeout=45)
    if code != 0:
        return None
    lines = [ln.replace("|", ": ", 1) for ln in out.splitlines() if ln.strip()]
    return "\n".join(lines)[:6000] or None


async def _files(agent_id: int) -> list[str]:
    from . import runner
    if not await runner._volume_exists(agent_id):
        return []
    cmd = ["docker", "run", "--rm", "--network", "none", "-v",
           f"{runner.agent_volume(agent_id)}:/ws:ro", "--entrypoint", "sh",
           config.TASK_IMAGE, "-c", _FILES_SH]
    code, out, _ = await verify.run_container(cmd, {}, timeout=30)
    return [ln.replace("\t", "  (") + " bytes)" for ln in out.splitlines() if ln.strip()] if code == 0 else []


async def gather_context(agent: dict) -> dict:
    org = agent["org_id"]
    sources = []
    for c in await db.list_org_db_connections(org):
        sources.append({"id": c["id"], "name": c["name"], "engine": c["engine"],
                        "schema": await _schema(c)})
    runs = [r for r in await db.list_runs(agent["id"], limit=8) if r.get("status") == "succeeded"][:3]
    recent = []
    for r in runs:
        full = await db.get_run(r["id"])
        recent.append({"run_id": r["id"], "date": (r.get("created_at") or "")[:10],
                       "result": (full.get("result") or "")[:1500]})
    owner = agent.get("owner_id")
    connected = [p for p in await db.active_providers(owner)] if owner else []
    return {"routine": {"name": agent["name"], "instructions": (agent.get("prompt") or "")[:3000],
                        "schedule": f"{agent.get('schedule_kind')} {agent.get('schedule_expr') or ''}".strip()},
            "data_sources": sources, "workspace_files": await _files(agent["id"]),
            "recent_successful_runs": recent,
            "delivery_channels_connected": sorted(set(connected) & {"slack", "gmail", "outlook"})}


_SYSTEM = """You design the definition of done for an AI agent's recurring job.
Return ONLY one JSON object, no prose, with this shape:
{
 "objective": "one sentence: the outcome, in the user's words",
 "checks": [ ... 1 to 6 checks ... ],
 "deliver_to": [ ... destinations ... ],
 "budget": {"max_attempts": 3},
 "notes": "short: assumptions you made, or what the user should confirm"
}

Checks are run independently AFTER the agent finishes, against the real systems,
read-only. They must PASS when the job really happened and FAIL when it did not.
Deterministic kinds (strength "verified") — always prefer these:
1. {"kind":"sql","name":"plain-language name","config":{"connection_id":<id from data_sources>,
    "query":"SELECT ... single read-only statement","expect":">= N" | "between A and B" | "nonempty" | "== N"},
    "required":true}
   - Query must return ONE value in the first column of the first row.
   - Use {date} for the run's UTC date (e.g. WHERE captured_date = '{date}').
   - Only use tables/columns that appear in that data source's schema.
   - Set thresholds about 10% below the healthy values you see in recent runs.
2. {"kind":"artifact","name":"...","config":{"path":"relative/path/in/workspace", plus any of:
    "non_empty":true, "modified_since_start":true, "contains":"text",
    "last_line_matches":"regex", "line_regex":"regex with (?P<ts>...ISO timestamp...)",
    "ts_after_start":true}, "required":true}
   - Only paths that exist in workspace_files.
Qualitative kinds — only for what a query or file check cannot prove:
3. {"kind":"judge","name":"...","config":{"rubric":["concrete, checkable criterion", ...1-6],
    "source":"outbox"|"result"|"file", "path":"only for file", "threshold":0.8},"required":true}
   - A separate model scores the output; each criterion must be answerable from the text
     alone ("names at least 3 competitors with prices", not "is insightful").
   - source "outbox" = the message staged for delivery (only if deliver_to has a message
     destination); "result" = the agent's final answer; "file" = a workspace file.
   - Shown to the user as JUDGED, never as verified.
4. {"kind":"human","name":"Owner approves before sending","config":{"mode":"approve"}} —
   ONLY if the user asked to review/approve before delivery. {"mode":"sample","rate":0.2}
   — only if they asked for spot-checks.

Destinations: always include {"kind":"notify"} (in-app). Add {"kind":"slack","channel":"#x"} or
{"kind":"email","to":"a@b.co"} ONLY if the user asked for it AND that channel is in
delivery_channels_connected. Data the agent writes itself: {"kind":"data","label":"what/where"}.

Prefer few, strong, deterministic checks over many weak ones. Never invent data sources,
tables, columns or files."""


def _user(ctx: dict, description: str) -> str:
    return ("WHAT THE USER WANTS (definition of done, in their words):\n"
            f"{description.strip()}\n\nGROUNDED CONTEXT (read-only):\n"
            + json.dumps(ctx, indent=1, default=str)[:24000])


async def draft(agent: dict, description: str) -> dict:
    """Ask the model for a draft spec, then validate every part of it."""
    ctx = await gather_context(agent)
    text = await llm.complete(_SYSTEM, _user(ctx, description), max_tokens=3500)
    raw = llm.extract_json(text)
    if not raw:
        raise llm.LLMError("the planner didn't return a usable draft — try rephrasing")
    warnings, checks = [], []
    for c in (raw.get("checks") or [])[:6]:
        try:
            checks.append(await verify.validate_check(c, agent["org_id"]))
        except ValueError as e:
            warnings.append(f"Dropped a check: {e}")
    dests = []
    for d in raw.get("deliver_to") or [{"kind": "notify"}]:
        try:
            dests += deliver.normalize_deliver_to([d])
        except ValueError as e:
            warnings.append(f"Dropped a destination: {e}")
    try:
        attempts = max(1, min(5, int((raw.get("budget") or {}).get("max_attempts") or 3)))
    except (TypeError, ValueError):
        attempts = 3
    if not checks:
        warnings.append("No runnable checks were proposed — add at least one before publishing.")
    return {
        "draft": {"objective": str(raw.get("objective") or description)[:2000],
                  "checks": checks, "deliver_to": dests or [{"kind": "notify"}],
                  "budget": {"max_attempts": attempts}},
        "notes": str(raw.get("notes") or "")[:1000],
        "warnings": warnings,
        "context": {"data_sources": [s["name"] for s in ctx["data_sources"]],
                    "files_seen": len(ctx["workspace_files"]),
                    "runs_seen": len(ctx["recent_successful_runs"]),
                    "channels": ctx["delivery_channels_connected"]},
    }


# ---------------- delegate a job: plan the whole routine, not just its checks ----------------

def _server_tz() -> str:
    import datetime as _dt
    now = _dt.datetime.now().astimezone()
    off = now.utcoffset() or _dt.timedelta(0)
    h, m = divmod(int(off.total_seconds()) // 60, 60)
    return f"{now.tzname()} (UTC{'+' if off >= _dt.timedelta(0) else '-'}{abs(h):02d}:{m:02d})"


_JOB_SYSTEM = """You turn a plain-language job into a complete, runnable job for an AI agent that
works in a sandboxed Linux workspace with Python, a shell, the web, and only the connectors
and data sources listed in the context. Return ONLY one JSON object:
{
 "name": "short name for the job (max 60 chars)",
 "instructions": "what the agent does EVERY time it runs — specific, step by step, naming the
   data sources/tables, files it writes (relative paths, e.g. output/report.md), and the
   format of results. Writes to databases must be idempotent (upsert on a natural key,
   never delete). Do not tell it to post/email results itself — delivery is done for it.",
 "schedule": {"kind": "once" | "cron", "expr": "5-field crontab or null",
              "human": "e.g. every weekday at 09:00"},
 "connectors": ["slugs from connectors_connected the job needs"],
 "data_source_ids": [ids from data_sources the job needs],
 "objective": "one sentence: the outcome, in the user's words",
 "checks": [ ...1 to 6 checks, as defined below... ],
 "deliver_to": [ ...destinations... ],
 "budget": {"max_attempts": 3},
 "notes": "assumptions, and anything the user must do first (e.g. connect Slack)"
}

SCHEDULE: cron runs in the server's timezone given in context. "once" = run one time now.

CHECKS run independently after the agent finishes, read-only; they must PASS only when the
job really happened. Prefer deterministic kinds:
1. {"kind":"sql","name":"...","config":{"connection_id":<id>,"query":"SELECT ... one value",
    "expect":">= N" | "between A and B" | "nonempty"},"required":true}
   Use {date} for the run's UTC date. Only tables/columns from that source's schema.
2. {"kind":"artifact","name":"...","config":{"path":"the relative path your instructions
    tell the agent to write","non_empty":true,"modified_since_start":true,
    "contains":"optional text"},"required":true}
3. {"kind":"judge","name":"...","config":{"rubric":["concrete criterion answerable from the
    text, quotable as evidence", ...1-6],"source":"file"|"outbox"|"result",
    "path":"for file","threshold":0.8},"required":true}  — only for qualitative content.
    Criteria must be positive and quotable ("names 3 competitors with prices"), never
    absence ("contains no X").
4. {"kind":"human","name":"...","config":{"mode":"approve"}} — only if the user asked to
   approve before sending; {"mode":"sample","rate":0.2} if they asked to spot-check.

DESTINATIONS: always {"kind":"notify"}. {"kind":"owner"} = email the user (only if
owner_email_available). {"kind":"slack","channel":"#x"} / {"kind":"email","to":"a@b.co"} only
if the user asked AND that app is in connectors_connected. {"kind":"data","label":"..."} for
data the agent writes itself.

Never invent connectors, data sources, tables or columns. If the job needs an app that
isn't connected, plan it anyway and say so in notes."""


async def gather_job_context(org_id: int, owner_id: int, chat: Optional[dict] = None) -> dict:
    from .connectors import get_connector
    sources = []
    for c in await db.list_org_db_connections(org_id):
        sources.append({"id": c["id"], "name": c["name"], "engine": c["engine"],
                        "schema": await _schema(c)})
    active = await db.active_providers(owner_id)
    try:
        known = {p["slug"]: p.get("label") for p in get_connector().providers()}
    except Exception:  # noqa: BLE001
        known = {}
    ctx = {"server_timezone": _server_tz(),
           "connectors_connected": sorted(active),
           "connectors_available_not_connected": sorted(set(known) - set(active)),
           "data_sources": sources,
           "owner_email_available": deliver.smtp_available()}
    if chat:   # "save as job": what the conversation actually did
        turns = []
        for r in reversed(await db.list_runs(chat["id"], limit=12)):
            full = await db.get_run(r["id"])
            turns.append({"user": (full.get("prompt") or "")[:1500],
                          "agent": (full.get("result") or full.get("error") or "")[:1500]})
        ctx["conversation"] = turns
        ctx["chat_workspace_files"] = await _files(chat["id"])
    return ctx


async def plan_job(org_id: int, owner_id: int, description: str,
                   chat: Optional[dict] = None) -> dict:
    """Plain-language job (or a chat that did it) → a reviewable job + definition of done."""
    from . import scheduler
    ctx = await gather_job_context(org_id, owner_id, chat)
    ask = description.strip() or "Turn what this conversation did into a recurring job."
    user = ("THE JOB, in the user's words:\n" + ask + "\n\nGROUNDED CONTEXT (read-only):\n"
            + json.dumps(ctx, indent=1, default=str)[:30000])
    raw = llm.extract_json(await llm.complete(_JOB_SYSTEM, user, max_tokens=5000, timeout=180))
    if not raw:
        raise llm.LLMError("the planner didn't return a usable plan — try rephrasing")
    warnings = []
    sched = raw.get("schedule") or {}
    kind, expr = sched.get("kind"), sched.get("expr")
    if kind == "cron":
        try:
            scheduler.validate_schedule("cron", expr)
        except Exception:  # noqa: BLE001
            warnings.append(f"The proposed schedule '{expr}' isn't valid — set it below.")
            kind, expr = "cron", "0 9 * * *"
    else:
        kind, expr = "once", None
    connected = set(ctx["connectors_connected"])
    conns = [c for c in raw.get("connectors") or [] if isinstance(c, str)]
    missing = [c for c in conns if c not in connected]
    if missing:
        warnings.append(f"Needs {', '.join(missing)} — connect {'it' if len(missing) == 1 else 'them'} "
                        "in Connectors first.")
    src_ids = {s["id"] for s in ctx["data_sources"]}
    dbs = []
    for i in raw.get("data_source_ids") or []:
        try:
            if int(i) in src_ids:
                dbs.append(int(i))
        except (TypeError, ValueError):
            pass
    checks = []
    for c in (raw.get("checks") or [])[:6]:
        try:
            checks.append(await verify.validate_check(c, org_id))
        except ValueError as e:
            warnings.append(f"Dropped a check: {e}")
    # a SQL check implies the routine needs that data source
    for c in checks:
        cid = c["config"].get("connection_id") if c["kind"] == "sql" else None
        if cid and cid not in dbs:
            dbs.append(cid)
    dests = []
    for d in raw.get("deliver_to") or [{"kind": "notify"}]:
        try:
            dests += deliver.normalize_deliver_to([d])
        except ValueError as e:
            warnings.append(f"Dropped a destination: {e}")
    if not checks:
        warnings.append("No runnable checks were proposed — add at least one before it goes live.")
    try:
        attempts = max(1, min(5, int((raw.get("budget") or {}).get("max_attempts") or 3)))
    except (TypeError, ValueError):
        attempts = 3
    return {
        "job": {"name": str(raw.get("name") or "New job")[:60],
                "prompt": str(raw.get("instructions") or "")[:20000],
                "schedule_kind": kind, "schedule_expr": expr,
                "schedule_human": str(sched.get("human") or "")[:80],
                "connectors": [c for c in conns if c in connected],
                "db_connection_ids": dbs},
        "draft": {"objective": str(raw.get("objective") or ask)[:2000], "checks": checks,
                  "deliver_to": dests or [{"kind": "notify"}],
                  "budget": {"max_attempts": attempts}},
        "notes": str(raw.get("notes") or "")[:1500],
        "warnings": warnings,
        "context": {"data_sources": [s["name"] for s in ctx["data_sources"]],
                    "connectors": ctx["connectors_connected"],
                    "owner_email": ctx["owner_email_available"],
                    "timezone": ctx["server_timezone"],
                    "from_chat": bool(chat)},
    }

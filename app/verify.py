"""Independent verification of an outcome's acceptance checks.

See docs/features/outcome-jobs.md. Checks never trust the agent's own report:
they run host-side (never inside the agent's container) against the real
systems. Data checks use the org's registered data source, forced read-only for
the session; artifact checks read the agent's workspace volume read-only.

v1 check kinds: `sql` (Postgres) and `artifact`. Each returns a result dict:
{check_id, name, kind, strength, required, passed, observed, expected, detail}.
"""
import asyncio
import datetime
import os
import posixpath
import re
from typing import Optional

from . import config, db
from .connectors import db_driver

SUPPORTED_KINDS = {"sql", "artifact"}

_CMP = re.compile(r"^\s*(>=|<=|==|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$")
_BETWEEN = re.compile(r"^\s*between\s+(-?\d+(?:\.\d+)?)\s+and\s+(-?\d+(?:\.\d+)?)\s*$", re.I)
_SELECT = re.compile(r"(?is)^\s*(select|with)\b")


# ---------------- expectations ----------------

def parse_expect(expect: str) -> bool:
    """True if `expect` is a supported comparison ('>= 16000', 'between 1 and 9', 'nonempty')."""
    e = (expect or "").strip().lower()
    return bool(e in ("nonempty", "true") or _CMP.match(e) or _BETWEEN.match(e))


def evaluate_expect(observed: Optional[str], expect: str) -> bool:
    e = (expect or "").strip().lower()
    if e == "nonempty":
        return observed not in (None, "")
    if e == "true":
        return (observed or "").strip().lower() in ("t", "true", "1", "yes")
    try:
        val = float(observed)
    except (TypeError, ValueError):
        return False
    m = _BETWEEN.match(e)
    if m:
        return float(m.group(1)) <= val <= float(m.group(2))
    m = _CMP.match(e)
    if not m:
        return False
    op, n = m.group(1), float(m.group(2))
    return {">=": val >= n, "<=": val <= n, ">": val > n, "<": val < n,
            "==": val == n, "=": val == n, "!=": val != n}[op]


def validate_select(query: str) -> Optional[str]:
    """Return an error message if `query` isn't a single read query, else None."""
    q = (query or "").strip().rstrip(";").strip()
    if not q:
        return "query is empty"
    if ";" in q:
        return "only a single statement is allowed"
    if not _SELECT.match(q):
        return "only SELECT / WITH queries are allowed"
    return None


def safe_rel_path(path: str) -> Optional[str]:
    """Normalize a workspace-relative path; None if it escapes the workspace."""
    p = (path or "").strip()
    if p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/"):
        return None
    norm = posixpath.normpath(p)
    if norm.startswith("..") or norm == ".":
        return None
    return norm


# ---------------- context ----------------

def render(s: str, ctx: dict) -> str:
    """Substitute the few system-generated values a check may reference."""
    return (s or "").replace("{date}", ctx["date"]).replace("{outcome_start}", ctx["outcome_start"])


def _parse_ts(s: str) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------- docker helper ----------------

async def run_container(cmd: list[str], inject: dict, timeout: int = 60) -> tuple[int, str, str]:
    """Run a throwaway container; secrets travel via value-less -e, never argv."""
    full = list(cmd)
    env = {**os.environ, **inject}
    proc = await asyncio.create_subprocess_exec(
        *full, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# ---------------- check kinds ----------------

async def _check_sql(cfg: dict, ctx: dict) -> dict:
    query = render(cfg.get("query", ""), ctx).strip().rstrip(";").strip()
    expect = cfg.get("expect") or "nonempty"
    res = {"expected": expect}
    err = validate_select(query)
    if err:
        return {**res, "passed": False, "detail": err}
    conn = await db.get_db_connection(int(cfg.get("connection_id") or 0), ctx["org_id"])
    if not conn:
        return {**res, "passed": False, "detail": "data source not found in this workspace"}
    if conn["engine"] != "postgres":
        return {**res, "passed": False, "detail": f"{conn['engine']} checks are not supported yet"}
    # Always read-only for verification, whatever the data source's own mode is.
    # Enforced INSIDE the transaction (psql -1 + SET TRANSACTION READ ONLY): the
    # DSN-level `options=-c default_transaction_read_only=on` from effective_dsn is
    # silently dropped by connection poolers (e.g. Supabase's), so on its own it
    # lets a data-modifying CTE (`WITH x AS (DELETE …) SELECT …`) through.
    dsn = db_driver.effective_dsn("postgres", db_driver.decrypt(conn["dsn_encrypted"]), "read_only")
    inner = ('psql "$SADSN" -qtA -1 -v ON_ERROR_STOP=1 -c "SET TRANSACTION READ ONLY" '
             '-c "SET LOCAL statement_timeout=30000" -c "$SAQUERY"')
    inject = {"SADSN": dsn, "SAQUERY": query, "PGCONNECT_TIMEOUT": "8"}
    cmd = ["docker", "run", "--rm", "--network", config.TASK_NETWORK,
           "--memory", "256m", "--pids-limit", "64", "--entrypoint", "sh"]
    for k in inject:
        cmd += ["-e", k]
    cmd += [config.TASK_IMAGE, "-c", inner]
    code, out, errtxt = await run_container(cmd, inject, timeout=60)
    if code != 0:
        return {**res, "passed": False,
                "detail": f"query failed: {(errtxt or out).strip()[-300:]}"}
    first = next((ln for ln in out.splitlines() if ln.strip()), "")
    observed = first.split("|")[0].strip() if first else ""
    return {**res, "observed": observed, "passed": evaluate_expect(observed, expect),
            "detail": f"{conn['name']}: {query[:200]}"}


async def _check_artifact(cfg: dict, ctx: dict) -> dict:
    from . import runner   # lazy: runner imports the outcome engine, which imports us
    path = safe_rel_path(render(cfg.get("path", ""), ctx))
    if not path:
        return {"passed": False, "detail": "path must be relative to the workspace"}
    if not await runner._volume_exists(ctx["agent_id"]):
        return {"passed": False, "detail": "the agent has no workspace yet"}
    inner = ('if [ -e "/ws/$P" ]; then echo EXISTS; stat -c "%Y %s" "/ws/$P"; '
             'echo ---TAIL---; tail -n 400 "/ws/$P"; else echo MISSING; fi')
    cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "128m",
           "-v", f"{runner.agent_volume(ctx['agent_id'])}:/ws:ro", "-e", "P",
           "alpine", "sh", "-c", inner]
    code, out, errtxt = await run_container(cmd, {"P": path}, timeout=30)
    if code != 0:
        return {"passed": False, "detail": f"could not read workspace: {errtxt.strip()[-200:]}"}
    lines = out.splitlines()
    if not lines or lines[0].strip() != "EXISTS":
        return {"passed": False, "observed": "missing", "expected": f"{path} exists",
                "detail": path}
    try:
        mtime_s, size_s = lines[1].split()
        mtime, size = int(mtime_s), int(size_s)
    except (IndexError, ValueError):
        return {"passed": False, "detail": "could not stat the file"}
    tail = lines[3:] if len(lines) > 3 else []
    start = _parse_ts(ctx["outcome_start"])
    end = _parse_ts(ctx.get("outcome_end") or "") or datetime.datetime.now(datetime.timezone.utc)

    fails, notes, expected = [], [], []
    if cfg.get("non_empty"):
        expected.append("non-empty")
        if size <= 0:
            fails.append("file is empty")
    if cfg.get("modified_since_start"):
        expected.append("modified during this run")
        if start and mtime < start.timestamp():
            fails.append("not modified during this run")
    if cfg.get("contains"):
        expected.append(f"contains '{cfg['contains']}'")
        if not any(cfg["contains"] in ln for ln in tail):
            fails.append(f"'{cfg['contains']}' not found")
    if cfg.get("last_line_matches"):
        expected.append("last line matches the pattern")
        last = next((ln for ln in reversed(tail) if ln.strip()), "")
        if not re.search(cfg["last_line_matches"], last):
            fails.append("last line does not match")
        notes.append(f"last line: {last.strip()[:120]}")
    if cfg.get("line_regex"):
        rx = re.compile(cfg["line_regex"])
        expected.append("a matching line"
                        + (" written during this run" if cfg.get("ts_after_start") else ""))
        hit = None
        for ln in reversed(tail):
            m = rx.search(ln)
            if not m:
                continue
            if cfg.get("ts_after_start") and "ts" in rx.groupindex:
                ts = _parse_ts(m.group("ts"))
                if not ts or (start and ts < start) or ts > end + datetime.timedelta(minutes=10):
                    continue
            hit = ln.strip()
            break
        if hit:
            notes.append(f"found: {hit[:120]}")
        else:
            fails.append("no matching line" + (" during this run" if cfg.get("ts_after_start") else ""))
    observed = "; ".join(notes) or f"{size} bytes"
    return {"passed": not fails, "observed": observed, "expected": "; ".join(expected) or "exists",
            "detail": path + (f" — {', '.join(fails)}" if fails else "")}


_RUNNERS = {"sql": _check_sql, "artifact": _check_artifact}


async def run_check(check: dict, ctx: dict) -> dict:
    base = {"check_id": check.get("id"), "name": check["name"], "kind": check["kind"],
            "strength": check.get("strength") or "verified",
            "required": bool(check.get("required", True))}
    fn = _RUNNERS.get(check["kind"])
    if not fn:
        return {**base, "passed": False, "detail": f"check kind '{check['kind']}' is not supported yet"}
    try:
        return {**base, **await fn(check.get("config") or {}, ctx)}
    except Exception as e:  # noqa: BLE001 - a broken check fails, never crashes the engine
        return {**base, "passed": False, "detail": f"verifier error: {type(e).__name__}: {e}"}


async def run_checks(checks: list[dict], ctx: dict) -> list[dict]:
    return list(await asyncio.gather(*(run_check(c, ctx) for c in checks)))

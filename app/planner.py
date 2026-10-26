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

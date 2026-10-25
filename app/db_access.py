"""What can a data source's login actually do? (read-only enforcement check)

The data-source `read_only` mode reaches agents two ways: a connection option
(`default_transaction_read_only`, see db_driver.effective_dsn) and an
instruction in the agent's prompt. Neither is a guarantee: connection poolers
(e.g. Supabase's) silently drop the option, and on a direct connection an agent
can turn it off with one SET. The only real guarantee is the database login's
own privileges — so we measure those, with a single read-only catalog query, and
report honestly.

Statuses:
  enforced        read_only source; the login has no write/create privileges
  session_only    read_only source; the login CAN write, but the session default
                  is read-only (an agent could switch it off)
  not_enforced    read_only source; the login CAN write and the read-only option
                  did not survive the connection (e.g. a pooler dropped it)
  write_access    read_write source (expected)
  unchecked       engine we can't assess yet (MySQL / Mongo)
  error           the check itself failed (unreachable, bad credentials, …)
"""
import json
from typing import Optional
from urllib.parse import urlparse

from . import config
from .connectors import db_driver
from .verify import run_container

# One row, pipe-separated. Pure catalog reads; also run inside a read-only txn.
_PROBE = """
SELECT current_user,
       current_database(),
       current_setting('default_transaction_read_only'),
       (SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user),
       (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r','p')
           AND n.nspname NOT IN ('pg_catalog','information_schema')
           AND n.nspname NOT LIKE 'pg\\_%'
           AND (has_table_privilege(c.oid,'INSERT') OR has_table_privilege(c.oid,'UPDATE')
                OR has_table_privilege(c.oid,'DELETE') OR has_table_privilege(c.oid,'TRUNCATE'))),
       has_database_privilege(current_database(),'CREATE')
         OR EXISTS (SELECT 1 FROM pg_namespace n
                     WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                       AND n.nspname NOT LIKE 'pg\\_%'
                       AND has_schema_privilege(n.oid,'CREATE')),
       (SELECT coalesce(string_agg(DISTINCT n.nspname, ','), '')
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r','p','v','m')
           AND n.nspname NOT IN ('pg_catalog','information_schema')
           AND n.nspname NOT LIKE 'pg\\_%')
""".strip()

# schemas that belong to a managed platform, never to grant an agent by default
_PLATFORM_SCHEMAS = {"auth", "storage", "realtime", "_realtime", "extensions", "graphql",
                     "graphql_public", "vault", "pgsodium", "pgsodium_masks", "net",
                     "supabase_functions", "supabase_migrations", "cron", "pgbouncer"}

LABELS = {
    "enforced": "Read-only · enforced",
    "session_only": "Read-only · not guaranteed",
    "not_enforced": "Read-only · not enforced",
    "write_access": "Read & write",
    "unchecked": "Read-only · not checked",
    "error": "Access not checked",
}


def _host(dsn: str) -> str:
    try:
        return urlparse(dsn).hostname or ""
    except ValueError:
        return ""


async def assess(engine: str, dsn: str, mode: str) -> dict:
    """Measure what this login can do. Never raises; never writes."""
    if engine != "postgres":
        st = "write_access" if mode == "read_write" else "unchecked"
        msg = ("Read & write." if st == "write_access" else
               f"Read-only isn't enforced for {engine} sources yet. Use a login that "
               "only has read permissions.")
        return {"status": st, "label": LABELS[st], "message": msg, "detail": {}}

    eff = db_driver.effective_dsn(engine, dsn, mode)   # exactly what an agent gets
    inner = ('psql "$SADSN" -qtA -1 -v ON_ERROR_STOP=1 -c "SET TRANSACTION READ ONLY" '
             '-c "SET LOCAL statement_timeout=20000" -c "$SAQUERY"')
    inject = {"SADSN": eff, "SAQUERY": _PROBE, "PGCONNECT_TIMEOUT": "8"}
    cmd = ["docker", "run", "--rm", "--network", config.TASK_NETWORK,
           "--memory", "256m", "--pids-limit", "64", "--entrypoint", "sh"]
    for k in inject:
        cmd += ["-e", k]
    cmd += [config.TASK_IMAGE, "-c", inner]
    code, out, err = await run_container(cmd, inject, timeout=45)
    if code != 0:
        return {"status": "error", "label": LABELS["error"],
                "message": f"Could not check this login's permissions: {(err or out).strip()[-200:]}",
                "detail": {}}
    row = next((ln for ln in out.splitlines() if ln.strip()), "").split("|")
    if len(row) < 7:
        return {"status": "error", "label": LABELS["error"],
                "message": "Unexpected answer from the permissions check.", "detail": {}}
    user, database, session_ro, elevated, writable, can_create, schemas = row[:7]
    detail = {
        "user": user, "database": database,
        "session_read_only": session_ro.strip() == "on",
        "elevated": elevated.strip() == "t",
        "writable_tables": int(writable or 0),
        "can_create": can_create.strip() == "t",
        "schemas": [s for s in schemas.split(",") if s],
        "host": _host(dsn),
    }
    can_write = detail["elevated"] or detail["writable_tables"] > 0 or detail["can_create"]
    n = detail["writable_tables"]
    what = (f"{'a superuser-level login' if detail['elevated'] else 'this login'} can write to "
            f"{n} table{'s' if n != 1 else ''}"
            + (" and create objects" if detail["can_create"] else ""))
    if mode != "read_only":
        st, msg = "write_access", "Read & write."
    elif not can_write:
        st, msg = "enforced", "The database login itself has no write permissions, so agents can only read."
    elif detail["session_read_only"]:
        st, msg = ("session_only",
                   f"Only the session default is read-only — {what}, and an agent could switch "
                   "that default off. Use a read-only login to guarantee it.")
    else:
        st, msg = ("not_enforced",
                   f"Not enforced — {what}, and the read-only setting doesn't survive this "
                   "connection (connection poolers drop it). Use a read-only login.")
    return {"status": st, "label": LABELS[st], "message": msg, "detail": detail}


def fix_sql(detail: dict) -> Optional[str]:
    """SQL that creates a SELECT-only login for this database (Postgres)."""
    if not detail or not detail.get("database"):
        return None
    db_ = detail["database"].replace('"', '""')
    schemas = [s for s in detail.get("schemas") or [] if s not in _PLATFORM_SCHEMAS] or ["public"]
    lines = [
        "-- Run as the role that owns your tables (e.g. the database owner).",
        "-- Choose your own strong password.",
        "CREATE ROLE antonagents_readonly LOGIN PASSWORD 'change-me';",
        f'GRANT CONNECT ON DATABASE "{db_}" TO antonagents_readonly;',
    ]
    for s in schemas:
        q = s.replace('"', '""')
        lines += [
            f'GRANT USAGE ON SCHEMA "{q}" TO antonagents_readonly;',
            f'GRANT SELECT ON ALL TABLES IN SCHEMA "{q}" TO antonagents_readonly;',
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{q}" GRANT SELECT ON TABLES TO antonagents_readonly;',
        ]
    lines.append("ALTER ROLE antonagents_readonly SET default_transaction_read_only = on;")
    if "supabase" in (detail.get("host") or ""):
        lines += ["",
                  "-- Supabase pooler: log in as  antonagents_readonly.<your-project-ref>",
                  "-- (same host and port), then update this data source's connection string."]
    else:
        lines += ["", "-- Then update this data source's connection string to use the new login."]
    return "\n".join(lines)


def to_json(a: dict) -> str:
    return json.dumps({"label": a.get("label"), "message": a.get("message"),
                       **(a.get("detail") or {})})

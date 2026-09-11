"""Async SQLite persistence: orgs, users, agents, runs, events, connections.

An **agent** is a persistent, reusable configuration (model, system prompt,
default instruction, schedule, connectors, skills). Each execution is a **run**
(a chat turn, a scheduled fire, or a manual trigger) with its own prompt. Every
JSON event a run's container emits is stored so a run can be replayed and
streamed. All rows are scoped to an **org** for multi-tenant isolation.

Enrichment history: `agents` supersedes the former `tasks` table; init() migrates
an old task-based DB in place (orgs backfilled per user, tasks -> agents, runs
repointed to agent_id, tasks dropped).
"""
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Optional

import aiosqlite

from . import config

_db: Optional[aiosqlite.Connection] = None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Final (current) schema. CREATE IF NOT EXISTS is a no-op on an existing table,
# so schema *changes* to pre-existing tables are handled by _migrate() below.
SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER REFERENCES orgs(id) ON DELETE CASCADE,
    role          TEXT NOT NULL DEFAULT 'admin',
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    password_hash TEXT,
    google_sub    TEXT UNIQUE,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash    TEXT NOT NULL UNIQUE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    active_org_id INTEGER REFERENCES orgs(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);

CREATE TABLE IF NOT EXISTS memberships (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     INTEGER NOT NULL REFERENCES orgs(id)  ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member',   -- 'admin' | 'member'
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_memberships_org  ON memberships(org_id);

CREATE TABLE IF NOT EXISTS invitations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id      INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member',
    token       TEXT NOT NULL UNIQUE,
    invited_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | revoked
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    accepted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invitations_token ON invitations(token);
CREATE INDEX IF NOT EXISTS idx_invitations_org   ON invitations(org_id);

CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER REFERENCES orgs(id) ON DELETE CASCADE,
    owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'routine',   -- 'routine' (scheduled/manual) | 'chat' (interactive thread)
    prompt        TEXT NOT NULL DEFAULT '',
    system_prompt TEXT,
    provider      TEXT,
    model         TEXT,
    schedule_kind TEXT NOT NULL DEFAULT 'once',
    schedule_expr TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    env_json      TEXT NOT NULL DEFAULT '{}',
    allowed_tools_json TEXT,
    max_turns     INTEGER,
    baseline_minutes INTEGER,
    last_session_id TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    prompt      TEXT,
    resume      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'queued',
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    exit_code   INTEGER,
    cost_usd    REAL,
    result      TEXT,
    error       TEXT,
    session_id  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    data    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id       INTEGER REFERENCES orgs(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,
    external_ref TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(user_id, provider)
);

CREATE TABLE IF NOT EXISTS skills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id       INTEGER REFERENCES orgs(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    description  TEXT,
    instructions TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_connectors (
    agent_id  INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    provider  TEXT NOT NULL,
    PRIMARY KEY (agent_id, provider)
);

CREATE TABLE IF NOT EXISTS agent_skills (
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    skill_id INTEGER NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    PRIMARY KEY (agent_id, skill_id)
);

CREATE TABLE IF NOT EXISTS db_connections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id        INTEGER REFERENCES orgs(id) ON DELETE CASCADE,
    owner_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    name          TEXT NOT NULL,
    engine        TEXT NOT NULL,
    dsn_encrypted TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'read_only',
    created_at    TEXT NOT NULL,
    UNIQUE(org_id, name)
);

CREATE TABLE IF NOT EXISTS agent_db_connections (
    agent_id         INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    db_connection_id INTEGER NOT NULL REFERENCES db_connections(id) ON DELETE CASCADE,
    PRIMARY KEY (agent_id, db_connection_id)
);

CREATE TABLE IF NOT EXISTS alert_rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,            -- on_failure | on_success | result_contains | schedule_miss
    condition  TEXT,                     -- substring to match for result_contains
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id     INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    agent_id   INTEGER REFERENCES agents(id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    title      TEXT NOT NULL,
    body       TEXT,
    read       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Per-agent API keys for programmatic access. The raw key is shown once at
-- creation and stored only as a hash (like sessions), so a DB leak doesn't hand
-- out live keys. `prefix` is a non-secret display snippet ("sk_ag_ab12…"). A key
-- is scoped to exactly one agent + org.
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash     TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL,
    agent_id     INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    org_id       INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name         TEXT,
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_connections_user ON connections(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_org ON agents(org_id);
CREATE INDEX IF NOT EXISTS idx_skills_org ON skills(org_id);
CREATE INDEX IF NOT EXISTS idx_alert_rules_agent ON alert_rules(agent_id);
CREATE INDEX IF NOT EXISTS idx_notifications_org ON notifications(org_id, read, id);
CREATE INDEX IF NOT EXISTS idx_api_keys_agent ON api_keys(agent_id);
"""

# Indexes on columns that may not exist until _migrate() runs (an old `runs`
# table lacks agent_id). Created after migration.
POST_MIGRATE_INDEXES = "CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);"


async def _cols(table: str) -> set:
    cur = await _db.execute(f"PRAGMA table_info({table})")
    return {r["name"] for r in await cur.fetchall()}


async def _tables() -> set:
    cur = await _db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in await cur.fetchall()}


async def init() -> None:
    global _db
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL;")
    await _db.execute("PRAGMA foreign_keys=ON;")
    await _db.executescript(SCHEMA)
    await _db.commit()
    await _migrate()
    await _ensure_column("agents", "baseline_minutes", "INTEGER")
    await _ensure_column("agents", "kind", "TEXT NOT NULL DEFAULT 'routine'")
    # stable, unguessable public id for the API surface (/v1/agents/<public_id>)
    await _ensure_column("agents", "public_id", "TEXT")
    # signup invites (own-org onboarding): NULL/0 = teammate join, 1 = create own org
    await _ensure_column("invitations", "new_workspace", "INTEGER NOT NULL DEFAULT 0")
    await _backfill_public_ids()
    await _db.executescript(POST_MIGRATE_INDEXES)
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_public_id ON agents(public_id)")
    await _db.commit()
    # backfill built-in skills (e.g. Artifact Design) into every existing org
    cur = await _db.execute("SELECT id FROM orgs")
    for row in await cur.fetchall():
        await _ensure_org_default_skills(row[0])
    # encrypt any agent env blobs still stored in plaintext (pre-encryption rows)
    await _encrypt_plaintext_env()
    # any run left 'running'/'queued' from a previous process crash — mark it
    await _db.execute(
        "UPDATE runs SET status='interrupted', finished_at=?, "
        "error='server restarted' WHERE status IN ('queued','running')",
        (_now(),),
    )
    await _db.commit()


async def _ensure_column(table: str, column: str, decl: str) -> None:
    if column not in await _cols(table):
        await _db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        await _db.commit()


async def _encrypt_plaintext_env() -> None:
    """One-time-per-row backfill: re-store any agent env still held in plaintext
    (rows written before at-rest encryption) as a Fernet token. Rows already
    encrypted decrypt cleanly and are left untouched, so this is idempotent."""
    from .connectors.db_driver import decrypt
    cur = await _db.execute("SELECT id, env_json FROM agents")
    rows = await cur.fetchall()
    changed = 0
    for r in rows:
        raw = r["env_json"]
        if not raw:
            continue
        try:
            decrypt(raw)          # already a valid Fernet token → leave as-is
            continue
        except Exception:
            pass                  # plaintext (incl. the '{}' default) → encrypt
        await _db.execute(
            "UPDATE agents SET env_json=? WHERE id=?",
            (_encrypt_env(_decrypt_env(raw)), r["id"]),
        )
        changed += 1
    if changed:
        await _db.commit()


async def _migrate() -> None:
    """Bring an older task-based DB up to the org/agent schema, idempotently."""
    # 1. org scoping columns on pre-existing tables
    await _ensure_column("users", "org_id", "INTEGER")
    await _ensure_column("users", "role", "TEXT NOT NULL DEFAULT 'admin'")
    await _ensure_column("connections", "org_id", "INTEGER")
    # teams: which org a session is currently acting in (used from step 2 on)
    await _ensure_column("sessions", "active_org_id", "INTEGER")

    # 2. one org per existing org-less user; backfill their connections' org_id
    cur = await _db.execute("SELECT id, name, email FROM users WHERE org_id IS NULL")
    for u in await cur.fetchall():
        label = (u["name"] or (u["email"] or "").split("@")[0] or "workspace")
        oc = await _db.execute("INSERT INTO orgs (name, created_at) VALUES (?,?)",
                               (f"{label}'s workspace", _now()))
        org_id = oc.lastrowid
        await _db.execute("UPDATE users SET org_id=? WHERE id=?", (org_id, u["id"]))
        await _db.execute("UPDATE connections SET org_id=? WHERE user_id=? AND org_id IS NULL",
                          (org_id, u["id"]))
    await _db.commit()

    # 2b. teams backfill: every existing user is an admin member of their own org.
    # Idempotent — re-runs skip rows that already exist.
    cur = await _db.execute("SELECT id, org_id, role FROM users WHERE org_id IS NOT NULL")
    for u in await cur.fetchall():
        await _db.execute(
            "INSERT OR IGNORE INTO memberships (user_id, org_id, role, created_at) VALUES (?,?,?,?)",
            (u["id"], u["org_id"], (u["role"] or "admin"), _now()),
        )
    await _db.commit()

    # 3. tasks -> agents, then rebuild runs onto agent_id and drop tasks
    tables = await _tables()
    if "tasks" not in tables:
        return

    # 3a. migrate each task to an agent, remembering the mapping
    task_to_agent: dict[int, int] = {}
    cur = await _db.execute("SELECT * FROM tasks")
    for t in await cur.fetchall():
        t = dict(t)
        owner_id = t.get("owner_id")
        org_id = None
        if owner_id is not None:
            r = await (await _db.execute("SELECT org_id FROM users WHERE id=?", (owner_id,))).fetchone()
            org_id = r["org_id"] if r else None
        ac = await _db.execute(
            """INSERT INTO agents
               (org_id, owner_id, name, prompt, system_prompt, provider, model,
                schedule_kind, schedule_expr, enabled, env_json, allowed_tools_json,
                max_turns, last_session_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (org_id, owner_id, t.get("title") or "Migrated agent", t.get("prompt") or "",
             t.get("system_prompt"), t.get("provider"), t.get("model"),
             t.get("schedule_kind") or "once", t.get("schedule_expr"),
             t.get("enabled", 1), t.get("env_json") or "{}", t.get("allowed_tools_json"),
             t.get("max_turns"), t.get("last_session_id"), t.get("created_at") or _now()),
        )
        task_to_agent[t["id"]] = ac.lastrowid
    await _db.commit()

    # 3b. rebuild runs with agent_id (drops the NOT NULL task_id FK). Preserve run ids.
    if "task_id" in await _cols("runs"):
        await _ensure_column("runs", "agent_id", "INTEGER")
        await _ensure_column("runs", "prompt", "TEXT")
        await _ensure_column("runs", "resume", "INTEGER NOT NULL DEFAULT 0")
        for task_id, agent_id in task_to_agent.items():
            await _db.execute("UPDATE runs SET agent_id=? WHERE task_id=?", (agent_id, task_id))
        await _db.commit()
        await _db.execute("PRAGMA foreign_keys=OFF;")
        await _db.executescript(
            """
            CREATE TABLE runs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                prompt TEXT, resume INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
                started_at TEXT, finished_at TEXT, exit_code INTEGER, cost_usd REAL,
                result TEXT, error TEXT, session_id TEXT
            );
            INSERT INTO runs_new (id, agent_id, prompt, resume, status, created_at,
                started_at, finished_at, exit_code, cost_usd, result, error, session_id)
              SELECT id, agent_id, prompt, COALESCE(resume,0), status, created_at,
                started_at, finished_at, exit_code, cost_usd, result, error, session_id
              FROM runs WHERE agent_id IS NOT NULL;
            DROP TABLE runs;
            ALTER TABLE runs_new RENAME TO runs;
            DROP TABLE tasks;
            CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id);
            CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
            """
        )
        await _db.commit()
        await _db.execute("PRAGMA foreign_keys=ON;")
        await _db.commit()


async def close() -> None:
    if _db is not None:
        await _db.close()


# ---------- orgs ----------

async def create_org(name: str) -> int:
    cur = await _db.execute("INSERT INTO orgs (name, created_at) VALUES (?,?)", (name, _now()))
    await _db.commit()
    org_id = cur.lastrowid
    await _ensure_org_default_skills(org_id)
    return org_id


async def get_org(org_id: int) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM orgs WHERE id=?", (org_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


# ---------- agents ----------

# An agent's `env` may hold user-supplied secrets (API tokens pasted into a
# routine). We encrypt the whole JSON blob at rest with the same Fernet scheme
# the DB connectors use (keyed off config.SECRET_KEY), decrypting only when the
# row is loaded for a run. `_decrypt_env` tolerates rows written before at-rest
# encryption existed (and the schema's plaintext '{}' default), so old DBs keep
# reading; a startup backfill (`_encrypt_plaintext_env`) re-encrypts them.
def _encrypt_env(env: Optional[dict]) -> str:
    from .connectors.db_driver import encrypt
    return encrypt(json.dumps(env or {}))


def _decrypt_env(stored: Optional[str]) -> dict:
    if not stored:
        return {}
    from .connectors.db_driver import decrypt
    try:
        return json.loads(decrypt(stored))
    except Exception:
        # legacy plaintext JSON (pre-encryption) or the default '{}' literal —
        # not a valid Fernet token, so decrypt() raised.
        try:
            return json.loads(stored)
        except Exception:
            return {}


def _agent_row(r: aiosqlite.Row, with_env: bool = True) -> dict:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    raw = d.pop("env_json")
    # env is only needed at run time (runner). Skip the per-row Fernet decrypt when
    # listing many agents (with_env=False) — it's wasted CPU on a hot path.
    d["env"] = _decrypt_env(raw) if with_env else {}
    at = d.pop("allowed_tools_json", None)
    d["allowed_tools"] = json.loads(at) if at else None
    return d


async def create_agent(data: dict, *, org_id: int, owner_id: int) -> int:
    cur = await _db.execute(
        """INSERT INTO agents
           (org_id, owner_id, name, kind, prompt, system_prompt, provider, model,
            schedule_kind, schedule_expr, enabled, env_json, allowed_tools_json,
            max_turns, baseline_minutes, public_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            org_id, owner_id, data["name"], data.get("kind") or "routine",
            data.get("prompt") or "",
            data.get("system_prompt"), data.get("provider"), data.get("model"),
            data.get("schedule_kind") or "once", data.get("schedule_expr"), 1,
            _encrypt_env(data.get("env")),
            json.dumps(data["allowed_tools"]) if data.get("allowed_tools") else None,
            data.get("max_turns"), data.get("baseline_minutes"), _gen_public_id(), _now(),
        ),
    )
    await _db.commit()
    return cur.lastrowid


async def get_agent(agent_id: int) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM agents WHERE id=?", (agent_id,))
    row = await cur.fetchone()
    return _agent_row(row) if row else None


async def get_owned_agent(agent_id: int, org_id: int) -> Optional[dict]:
    """Fetch an agent only if it belongs to org_id — the isolation primitive."""
    cur = await _db.execute(
        "SELECT * FROM agents WHERE id=? AND org_id=?", (agent_id, org_id)
    )
    row = await cur.fetchone()
    return _agent_row(row) if row else None


async def list_agents(org_id: int, kind: Optional[str] = None) -> list[dict]:
    if kind:
        cur = await _db.execute(
            "SELECT * FROM agents WHERE org_id=? AND kind=? ORDER BY id DESC", (org_id, kind)
        )
    else:
        cur = await _db.execute(
            "SELECT * FROM agents WHERE org_id=? ORDER BY id DESC", (org_id,)
        )
    return [_agent_row(r, with_env=False) for r in await cur.fetchall()]


async def list_scheduled_agents() -> list[dict]:
    cur = await _db.execute(
        "SELECT * FROM agents WHERE enabled=1 AND schedule_kind IN ('cron','interval')"
    )
    return [_agent_row(r) for r in await cur.fetchall()]


async def set_agent_enabled(agent_id: int, enabled: bool) -> None:
    await _db.execute("UPDATE agents SET enabled=? WHERE id=?", (1 if enabled else 0, agent_id))
    await _db.commit()


async def set_agent_schedule(agent_id: int, kind: str, expr: Optional[str]) -> None:
    await _db.execute(
        "UPDATE agents SET schedule_kind=?, schedule_expr=? WHERE id=?",
        (kind, expr, agent_id),
    )
    await _db.commit()


async def set_agent_name(agent_id: int, name: str) -> None:
    await _db.execute("UPDATE agents SET name=? WHERE id=?", (name, agent_id))
    await _db.commit()


async def set_agent_model(agent_id: int, provider: Optional[str], model: Optional[str]) -> None:
    """Pin an agent/chat to a provider+model (both None => use the routed default)."""
    await _db.execute(
        "UPDATE agents SET provider=?, model=? WHERE id=?", (provider, model, agent_id))
    await _db.commit()


# ---------- public agent ids + API keys (programmatic access) ----------

def _gen_public_id() -> str:
    return "ag_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:16]


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _backfill_public_ids() -> None:
    """Give every existing agent a stable public_id (one-time)."""
    cur = await _db.execute("SELECT id FROM agents WHERE public_id IS NULL OR public_id=''")
    rows = await cur.fetchall()
    for r in rows:
        # loop to avoid the (astronomically unlikely) unique collision
        for _ in range(5):
            pid = _gen_public_id()
            exists = await (await _db.execute("SELECT 1 FROM agents WHERE public_id=?", (pid,))).fetchone()
            if not exists:
                await _db.execute("UPDATE agents SET public_id=? WHERE id=?", (pid, r["id"]))
                break
    if rows:
        await _db.commit()


async def get_agent_by_public_id(public_id: str) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM agents WHERE public_id=?", (public_id,))
    row = await cur.fetchone()
    return _agent_row(row) if row else None


async def create_api_key(agent_id: int, org_id: int, *, name: Optional[str],
                         created_by: Optional[int]) -> tuple[str, dict]:
    """Mint a scoped API key for an agent. Returns (raw_key, row). The raw key is
    returned ONCE — only its hash is stored."""
    raw = "sk_ag_" + secrets.token_urlsafe(32)
    prefix = raw[:14] + "…"          # e.g. "sk_ag_ab12cd…" (non-secret display)
    cur = await _db.execute(
        """INSERT INTO api_keys (key_hash, prefix, agent_id, org_id, name, created_by, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (_hash_key(raw), prefix, agent_id, org_id, name, created_by, _now()),
    )
    await _db.commit()
    row = {"id": cur.lastrowid, "prefix": prefix, "agent_id": agent_id,
           "name": name, "created_at": _now(), "revoked": False}
    return raw, row


async def resolve_api_key(raw: str) -> Optional[dict]:
    """Look up a live (non-revoked) API key by its raw value; touch last_used_at.
    Returns {id, agent_id, org_id} or None."""
    if not raw:
        return None
    cur = await _db.execute(
        "SELECT id, agent_id, org_id FROM api_keys WHERE key_hash=? AND revoked=0",
        (_hash_key(raw),))
    row = await cur.fetchone()
    if not row:
        return None
    await _db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (_now(), row["id"]))
    await _db.commit()
    return dict(row)


async def list_api_keys(agent_id: int) -> list[dict]:
    """Non-secret metadata for an agent's keys (never the hash)."""
    cur = await _db.execute(
        """SELECT id, prefix, name, created_at, last_used_at, revoked
           FROM api_keys WHERE agent_id=? ORDER BY id DESC""", (agent_id,))
    out = []
    for r in await cur.fetchall():
        d = dict(r); d["revoked"] = bool(d["revoked"]); out.append(d)
    return out


async def revoke_api_key(key_id: int, agent_id: int) -> bool:
    cur = await _db.execute(
        "UPDATE api_keys SET revoked=1 WHERE id=? AND agent_id=?", (key_id, agent_id))
    await _db.commit()
    return cur.rowcount > 0


async def set_agent_session(agent_id: int, session_id: str) -> None:
    await _db.execute("UPDATE agents SET last_session_id=? WHERE id=?", (session_id, agent_id))
    await _db.commit()


async def delete_agent(agent_id: int) -> None:
    await _db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
    await _db.commit()


# ---------- agent <-> connectors / skills binding ----------

async def set_agent_connectors(agent_id: int, providers: list[str]) -> None:
    await _db.execute("DELETE FROM agent_connectors WHERE agent_id=?", (agent_id,))
    for p in providers:
        await _db.execute(
            "INSERT OR IGNORE INTO agent_connectors (agent_id, provider) VALUES (?,?)",
            (agent_id, p),
        )
    await _db.commit()


async def get_agent_connectors(agent_id: int) -> list[str]:
    cur = await _db.execute(
        "SELECT provider FROM agent_connectors WHERE agent_id=? ORDER BY provider", (agent_id,)
    )
    return [r["provider"] for r in await cur.fetchall()]


# Built-in skill seeded into every org and auto-applied on every run (loaded on
# demand by the SDK, so it only kicks in when the agent is producing HTML).
ARTIFACT_DESIGN_SKILL = {
    "name": "Artifact Design",
    "description": ("Produce a polished, self-contained HTML artifact (report, page, "
                    "dashboard, poster) with real design: considered typography, palette, "
                    "layout, responsiveness, and light/dark theming."),
    "instructions": (
        "Use this whenever you create an HTML deliverable. Treat it as a designed page, not a "
        "plain document, and make it fully SELF-CONTAINED (inline all CSS; embed small images as "
        "data URIs) so it renders standalone. Save the finished file into ./artifacts/.\n\n"
        "Design principles:\n"
        "- Typography: pick a deliberate pairing — a characterful display face for headings, a clean "
        "face for body — using robust system stacks (e.g. ui-serif/Georgia for display; "
        "system-ui/-apple-system/'Segoe UI'/Roboto for body). Set a clear type scale, body "
        "line-height ~1.5, reading width ~65ch, and `text-wrap: balance` on headings.\n"
        "- Color: choose a small, intentional palette (one grounded neutral + one accent + a few "
        "supporting shades) that fits the subject; bias neutrals slightly toward a hue rather than "
        "pure gray. Avoid the generic 'AI look' (cream+terracotta, purple→blue hero gradients, "
        "everything centered, emoji as section bullets).\n"
        "- Layout: CSS grid/flexbox with `gap` for spacing (not stacked margins); give content room; "
        "responsive (relative units, `max-width:100%` on media, wrap wide tables/code in "
        "`overflow-x:auto`); the page must never scroll sideways.\n"
        "- Theme: support light AND dark via `@media (prefers-color-scheme)` driving CSS custom "
        "properties for the palette.\n"
        "- Polish: real hierarchy and spacing, `font-variant-numeric: tabular-nums` for aligned "
        "figures, visible :focus states, `prefers-reduced-motion` respected. Match intensity to the "
        "content — a report is restrained, a poster can be bold. Don't over-decorate.\n"
        "- Data/dashboards: summary first; encode state with color/badges; give charts an area fill "
        "and a faint grid.\n"
        "- Use the real content, never lorem; write clear, specific copy.\n"
        "Do NOT include <script>: artifacts are viewed sandboxed and scripts will not run — achieve "
        "everything with HTML + CSS."
    ),
}


async def _ensure_org_default_skills(org_id: int) -> None:
    """Idempotently seed an org's built-in skills (currently: Artifact Design)."""
    cur = await _db.execute(
        "SELECT 1 FROM skills WHERE org_id=? AND name=? LIMIT 1",
        (org_id, ARTIFACT_DESIGN_SKILL["name"]),
    )
    if await cur.fetchone():
        return
    await _db.execute(
        "INSERT INTO skills (org_id, name, description, instructions, created_at) VALUES (?,?,?,?,?)",
        (org_id, ARTIFACT_DESIGN_SKILL["name"], ARTIFACT_DESIGN_SKILL["description"],
         ARTIFACT_DESIGN_SKILL["instructions"], _now()),
    )
    await _db.commit()


async def get_skill_by_name(org_id: int, name: str) -> Optional[dict]:
    cur = await _db.execute(
        "SELECT * FROM skills WHERE org_id=? AND name=? LIMIT 1", (org_id, name),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def create_skill(org_id: int, name: str, *, description: Optional[str] = None,
                       instructions: str = "") -> int:
    cur = await _db.execute(
        """INSERT INTO skills (org_id, name, description, instructions, created_at)
           VALUES (?,?,?,?,?)""",
        (org_id, name, description, instructions, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def list_skills(org_id: int) -> list[dict]:
    cur = await _db.execute(
        "SELECT * FROM skills WHERE org_id=? ORDER BY name", (org_id,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def set_agent_skills(agent_id: int, skill_ids: list[int]) -> None:
    await _db.execute("DELETE FROM agent_skills WHERE agent_id=?", (agent_id,))
    for sid in skill_ids:
        await _db.execute(
            "INSERT OR IGNORE INTO agent_skills (agent_id, skill_id) VALUES (?,?)",
            (agent_id, sid),
        )
    await _db.commit()


async def get_agent_skills(agent_id: int) -> list[dict]:
    cur = await _db.execute(
        """SELECT skills.* FROM agent_skills JOIN skills ON agent_skills.skill_id = skills.id
           WHERE agent_skills.agent_id=? ORDER BY skills.name""",
        (agent_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ---------- database connections (native connectors) ----------

async def create_db_connection(org_id: int, owner_id: int, *, name: str, engine: str,
                               dsn_encrypted: str, mode: str = "read_only") -> int:
    cur = await _db.execute(
        """INSERT INTO db_connections (org_id, owner_id, name, engine, dsn_encrypted, mode, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, owner_id, name, engine, dsn_encrypted, mode, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def update_db_connection(conn_id: int, org_id: int, *, name: Optional[str] = None,
                               dsn_encrypted: Optional[str] = None,
                               mode: Optional[str] = None) -> None:
    """Update the given fields of an org's data source (e.g. rotate credentials)."""
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name)
    if dsn_encrypted is not None:
        sets.append("dsn_encrypted=?"); vals.append(dsn_encrypted)
    if mode is not None:
        sets.append("mode=?"); vals.append(mode)
    if not sets:
        return
    vals += [conn_id, org_id]
    await _db.execute(
        f"UPDATE db_connections SET {', '.join(sets)} WHERE id=? AND org_id=?", vals)
    await _db.commit()


async def list_db_connections(org_id: int) -> list[dict]:
    cur = await _db.execute(
        "SELECT id, org_id, owner_id, name, engine, mode, created_at FROM db_connections "
        "WHERE org_id=? ORDER BY name", (org_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_owner_db_connections(owner_id: int) -> list[dict]:
    """Full rows (incl. dsn_encrypted) for every data source this user owns."""
    cur = await _db.execute(
        "SELECT * FROM db_connections WHERE owner_id=? ORDER BY name", (owner_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_org_db_connections(org_id: int) -> list[dict]:
    """Full rows (incl. dsn_encrypted) for every data source in an org. Data sources
    are a shared team asset, so a chat auto-provisions all of its org's databases."""
    cur = await _db.execute(
        "SELECT * FROM db_connections WHERE org_id=? ORDER BY name", (org_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_db_connection(conn_id: int, org_id: int) -> Optional[dict]:
    """Full row incl. dsn_encrypted — for the runner to decrypt at launch."""
    cur = await _db.execute(
        "SELECT * FROM db_connections WHERE id=? AND org_id=?", (conn_id, org_id)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def delete_db_connection(conn_id: int, org_id: int) -> None:
    await _db.execute("DELETE FROM db_connections WHERE id=? AND org_id=?", (conn_id, org_id))
    await _db.commit()


async def set_agent_db_connections(agent_id: int, conn_ids: list[int]) -> None:
    await _db.execute("DELETE FROM agent_db_connections WHERE agent_id=?", (agent_id,))
    for cid in conn_ids:
        await _db.execute(
            "INSERT OR IGNORE INTO agent_db_connections (agent_id, db_connection_id) VALUES (?,?)",
            (agent_id, cid),
        )
    await _db.commit()


async def get_agent_db_connections(agent_id: int) -> list[dict]:
    """Full rows (incl. dsn_encrypted) for the agent's bound DB connections."""
    cur = await _db.execute(
        """SELECT db_connections.* FROM agent_db_connections
           JOIN db_connections ON agent_db_connections.db_connection_id = db_connections.id
           WHERE agent_db_connections.agent_id=? ORDER BY db_connections.name""",
        (agent_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


# ---------- runs ----------

async def create_run(agent_id: int, *, prompt: Optional[str] = None, resume: bool = False) -> int:
    cur = await _db.execute(
        "INSERT INTO runs (agent_id, prompt, resume, status, created_at) VALUES (?,?,?,?,?)",
        (agent_id, prompt, 1 if resume else 0, "queued", _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def mark_run_started(run_id: int) -> None:
    await _db.execute(
        "UPDATE runs SET status='running', started_at=? WHERE id=?", (_now(), run_id)
    )
    await _db.commit()


async def finish_run(run_id: int, *, status: str, exit_code: Optional[int] = None,
                     cost_usd: Optional[float] = None, result: Optional[str] = None,
                     error: Optional[str] = None) -> None:
    await _db.execute(
        """UPDATE runs SET status=?, finished_at=?, exit_code=?, cost_usd=?,
           result=COALESCE(?, result), error=COALESCE(?, error) WHERE id=?""",
        (status, _now(), exit_code, cost_usd, result, error, run_id),
    )
    await _db.commit()


async def set_run_session_id(run_id: int, session_id: str) -> None:
    await _db.execute("UPDATE runs SET session_id=? WHERE id=?", (session_id, run_id))
    await _db.commit()


async def get_run(run_id: int) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM runs WHERE id=?", (run_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_owned_run(run_id: int, org_id: int) -> Optional[dict]:
    """Fetch a run only if its agent belongs to org_id."""
    cur = await _db.execute(
        """SELECT runs.* FROM runs JOIN agents ON runs.agent_id = agents.id
           WHERE runs.id=? AND agents.org_id=?""",
        (run_id, org_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_runs(agent_id: int, limit: int = 100) -> list[dict]:
    # capped: a long-lived routine accumulates runs forever; the UI only shows a
    # short recent history and impact is a separate aggregate (agent_impact).
    cur = await _db.execute(
        "SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT ?", (agent_id, limit)
    )
    return [dict(r) for r in await cur.fetchall()]


async def last_runs_for(agent_ids: list[int]) -> dict:
    """Most-recent run per agent for a set of agents, in ONE query — avoids the
    N+1 of calling last_run() per agent when listing chats/routines."""
    if not agent_ids:
        return {}
    ph = ",".join("?" * len(agent_ids))
    cur = await _db.execute(
        f"SELECT r.* FROM runs r JOIN (SELECT agent_id, MAX(id) mid FROM runs "
        f"WHERE agent_id IN ({ph}) GROUP BY agent_id) m ON r.id = m.mid",
        agent_ids,
    )
    return {row["agent_id"]: dict(row) for row in await cur.fetchall()}


async def last_run(agent_id: int) -> Optional[dict]:
    cur = await _db.execute(
        "SELECT * FROM runs WHERE agent_id=? ORDER BY id DESC LIMIT 1", (agent_id,)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


# ---------- impact ----------

async def agent_impact(agent_id: int, baseline_minutes: Optional[int]) -> dict:
    """Per-agent impact: runs completed end-to-end, minutes saved, and spend."""
    cur = await _db.execute(
        """SELECT count(*) total,
                  coalesce(sum(CASE WHEN status='succeeded' THEN 1 ELSE 0 END),0) ok,
                  coalesce(sum(cost_usd),0) spend
           FROM runs WHERE agent_id=?""",
        (agent_id,),
    )
    r = await cur.fetchone()
    bm = baseline_minutes or 0
    return {"runs": r["total"], "runs_succeeded": r["ok"],
            "minutes_saved": r["ok"] * bm, "spend_usd": round(r["spend"], 4)}


# ---------- observability: ops health + activity/audit ----------

async def ops_health(org_id: int, since_iso: str) -> dict:
    """Org-wide run health over a window: status mix, success rate, latency
    (seconds), cost, recent errors, and a per-agent rollup."""
    cur = await _db.execute(
        """SELECT r.status s, count(*) n, coalesce(sum(r.cost_usd),0) cost
           FROM runs r JOIN agents a ON a.id=r.agent_id
           WHERE a.org_id=? AND r.created_at >= ? GROUP BY r.status""",
        (org_id, since_iso))
    by_status = {r["s"]: {"n": r["n"], "cost": r["cost"]} for r in await cur.fetchall()}
    total = sum(v["n"] for v in by_status.values())
    ok = by_status.get("succeeded", {}).get("n", 0)
    cost = round(sum(v["cost"] for v in by_status.values()), 4)
    # latency percentiles computed in python (SQLite has no percentile fn)
    cur = await _db.execute(
        """SELECT (julianday(r.finished_at)-julianday(r.started_at))*86400 secs
           FROM runs r JOIN agents a ON a.id=r.agent_id
           WHERE a.org_id=? AND r.created_at >= ?
             AND r.started_at IS NOT NULL AND r.finished_at IS NOT NULL""",
        (org_id, since_iso))
    durs = sorted(x["secs"] for x in await cur.fetchall() if x["secs"] is not None and x["secs"] >= 0)
    def _pct(p):
        if not durs: return 0.0
        return round(durs[min(len(durs) - 1, int(round((p / 100) * (len(durs) - 1))))], 1)
    latency = {"avg": round(sum(durs) / len(durs), 1) if durs else 0.0,
               "p50": _pct(50), "p95": _pct(95), "max": round(durs[-1], 1) if durs else 0.0}
    # group identical failures into incidents (same agent + same error) with a count
    cur = await _db.execute(
        """SELECT r.agent_id, a.name agent_name, r.error, r.status,
                  count(*) n, max(r.id) last_run, max(r.finished_at) last_at
           FROM runs r JOIN agents a ON a.id=r.agent_id
           WHERE a.org_id=? AND r.status IN ('failed','interrupted') AND r.created_at >= ?
           GROUP BY r.agent_id, r.error ORDER BY last_run DESC LIMIT 12""", (org_id, since_iso))
    errors = [{"agent_id": r["agent_id"], "agent_name": r["agent_name"],
               "status": r["status"], "error": (r["error"] or r["status"] or "")[:300],
               "count": r["n"], "last_run": r["last_run"], "at": r["last_at"]}
              for r in await cur.fetchall()]
    cur = await _db.execute(
        """SELECT a.id, a.name, count(*) runs,
                  sum(CASE WHEN r.status='succeeded' THEN 1 ELSE 0 END) ok,
                  avg((julianday(r.finished_at)-julianday(r.started_at))*86400) avg_secs,
                  coalesce(sum(r.cost_usd),0) cost
           FROM runs r JOIN agents a ON a.id=r.agent_id
           WHERE a.org_id=? AND r.created_at >= ?
           GROUP BY a.id ORDER BY runs DESC LIMIT 25""", (org_id, since_iso))
    agents = [{"id": r["id"], "name": r["name"], "runs": r["runs"], "ok": r["ok"] or 0,
               "success_rate": round(100 * (r["ok"] or 0) / r["runs"]) if r["runs"] else 0,
               "avg_secs": round(r["avg_secs"], 1) if r["avg_secs"] else 0.0,
               "cost": round(r["cost"], 4)} for r in await cur.fetchall()]
    return {"total": total, "succeeded": ok,
            "failed": by_status.get("failed", {}).get("n", 0),
            "interrupted": by_status.get("interrupted", {}).get("n", 0),
            "running": by_status.get("running", {}).get("n", 0) + by_status.get("queued", {}).get("n", 0),
            "success_rate": round(100 * ok / total) if total else 0,
            "cost_usd": cost, "latency": latency, "errors": errors, "agents": agents}


async def live_status(org_id: int) -> dict:
    """Live 'is the machine working' signal for the org: in-flight runs (with the
    agent name + start time so the UI can show elapsed) and routines whose most
    recent run failed (currently broken). No cost — that number isn't accurate yet."""
    cur = await _db.execute(
        """SELECT r.id run_id, r.agent_id, a.name, r.started_at, r.status
           FROM runs r JOIN agents a ON a.id=r.agent_id
           WHERE a.org_id=? AND r.status IN ('running','queued')
           ORDER BY r.started_at""", (org_id,))
    running = [{"run_id": x["run_id"], "agent_id": x["agent_id"], "name": x["name"],
                "started_at": x["started_at"], "status": x["status"]} for x in await cur.fetchall()]
    # routines whose LATEST run failed/interrupted = currently broken
    cur = await _db.execute(
        """SELECT a.id agent_id, a.name, r.id run_id, r.error, r.status, r.finished_at
           FROM agents a JOIN runs r
             ON r.id = (SELECT id FROM runs WHERE agent_id=a.id ORDER BY id DESC LIMIT 1)
           WHERE a.org_id=? AND a.kind='routine' AND r.status IN ('failed','interrupted')
           ORDER BY r.id DESC LIMIT 6""", (org_id,))
    broken = [{"run_id": x["run_id"], "agent_id": x["agent_id"], "name": x["name"],
               "error": (x["error"] or x["status"] or "")[:120], "at": x["finished_at"]}
              for x in await cur.fetchall()]
    return {"running": running, "running_count": len(running), "broken": broken}


async def activity(org_id: int, since_iso: str, agent_id: Optional[int] = None,
                   limit: int = 100, offset: int = 0) -> list[dict]:
    """Raw tool_use activity across the org's runs, newest first (the audit feed).
    The caller classifies each row into shell/web/files/app/task."""
    q = ("SELECT e.id, e.ts, e.run_id, e.data, r.agent_id, a.name agent_name "
         "FROM events e JOIN runs r ON r.id=e.run_id JOIN agents a ON a.id=r.agent_id "
         "WHERE a.org_id=? AND e.type='tool_use' AND e.ts >= ?")
    args: list = [org_id, since_iso]
    if agent_id is not None:
        q += " AND r.agent_id=?"; args.append(agent_id)
    q += " ORDER BY e.id DESC LIMIT ? OFFSET ?"; args += [limit, offset]
    cur = await _db.execute(q, args)
    return [dict(r) for r in await cur.fetchall()]


async def org_impact(org_id: int) -> dict:
    """Org rollup across all agents: minutes saved, successful runs, spend."""
    cur = await _db.execute(
        """SELECT coalesce(sum(CASE WHEN r.status='succeeded'
                       THEN coalesce(a.baseline_minutes,0) ELSE 0 END),0) minutes,
                  coalesce(sum(CASE WHEN r.status='succeeded' THEN 1 ELSE 0 END),0) ok,
                  coalesce(sum(r.cost_usd),0) spend
           FROM agents a LEFT JOIN runs r ON r.agent_id = a.id
           WHERE a.org_id=?""",
        (org_id,),
    )
    r = await cur.fetchone()
    ac = await (await _db.execute("SELECT count(*) c FROM agents WHERE org_id=?", (org_id,))).fetchone()
    return {"minutes_saved": r["minutes"], "runs_succeeded": r["ok"],
            "spend_usd": round(r["spend"], 4), "agents": ac["c"]}


# ---------- alerts & notifications ----------

async def set_agent_alerts(agent_id: int, org_id: int, rules: list[dict]) -> None:
    """Replace all alert rules for an agent (rebind semantics, like connectors/skills)."""
    await _db.execute("DELETE FROM alert_rules WHERE agent_id=?", (agent_id,))
    for r in rules:
        await _db.execute(
            """INSERT INTO alert_rules (agent_id, org_id, kind, condition, enabled, created_at)
               VALUES (?,?,?,?,?,?)""",
            (agent_id, org_id, r["kind"], r.get("condition"),
             1 if r.get("enabled", True) else 0, _now()),
        )
    await _db.commit()


async def get_agent_alerts(agent_id: int) -> list[dict]:
    cur = await _db.execute(
        "SELECT id, kind, condition, enabled FROM alert_rules WHERE agent_id=? ORDER BY id",
        (agent_id,),
    )
    return [{"id": r["id"], "kind": r["kind"], "condition": r["condition"],
             "enabled": bool(r["enabled"])} for r in await cur.fetchall()]


async def add_notification(org_id: int, agent_id: Optional[int], run_id: Optional[int],
                           title: str, body: str = "") -> int:
    cur = await _db.execute(
        """INSERT INTO notifications (org_id, agent_id, run_id, title, body, read, created_at)
           VALUES (?,?,?,?,?,0,?)""",
        (org_id, agent_id, run_id, title, body, _now()),
    )
    await _db.commit()
    return cur.lastrowid


async def list_notifications(org_id: int, *, limit: int = 50,
                             unread_only: bool = False) -> list[dict]:
    q = "SELECT id, agent_id, run_id, title, body, read, created_at FROM notifications WHERE org_id=?"
    if unread_only:
        q += " AND read=0"
    q += " ORDER BY id DESC LIMIT ?"
    cur = await _db.execute(q, (org_id, limit))
    return [{"id": r["id"], "agent_id": r["agent_id"], "run_id": r["run_id"],
             "title": r["title"], "body": r["body"], "read": bool(r["read"]),
             "created_at": r["created_at"]} for r in await cur.fetchall()]


async def unread_count(org_id: int) -> int:
    cur = await _db.execute(
        "SELECT count(*) c FROM notifications WHERE org_id=? AND read=0", (org_id,))
    return (await cur.fetchone())["c"]


async def mark_notifications_read(org_id: int, ids: Optional[list[int]] = None) -> None:
    """Mark specific notifications read, or all of the org's when ids is None."""
    if ids is None:
        await _db.execute("UPDATE notifications SET read=1 WHERE org_id=?", (org_id,))
    elif ids:
        marks = ",".join("?" for _ in ids)
        await _db.execute(
            f"UPDATE notifications SET read=1 WHERE org_id=? AND id IN ({marks})",
            (org_id, *ids),
        )
    await _db.commit()


# ---------- events ----------

async def add_event(run_id: int, seq: int, etype: str, data: dict) -> None:
    await _db.execute(
        "INSERT INTO events (run_id, seq, ts, type, data) VALUES (?,?,?,?,?)",
        (run_id, seq, _now(), etype, json.dumps(data, default=str)),
    )
    await _db.commit()


async def get_events(run_id: int) -> list[dict]:
    cur = await _db.execute(
        "SELECT seq, ts, type, data FROM events WHERE run_id=? ORDER BY seq", (run_id,)
    )
    out = []
    for r in await cur.fetchall():
        out.append({"seq": r["seq"], "ts": r["ts"], "type": r["type"],
                    **json.loads(r["data"])})
    return out


# ---------- users & sessions ----------

async def create_user(email: str, *, org_id: int, name: Optional[str] = None,
                      password_hash: Optional[str] = None,
                      google_sub: Optional[str] = None, role: str = "admin") -> int:
    cur = await _db.execute(
        """INSERT INTO users (org_id, role, email, name, password_hash, google_sub, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (org_id, role, email.lower(), name, password_hash, google_sub, _now()),
    )
    uid = cur.lastrowid
    # the creator of a fresh personal org is its admin member (teams model)
    await _db.execute(
        "INSERT OR IGNORE INTO memberships (user_id, org_id, role, created_at) VALUES (?,?,?,?)",
        (uid, org_id, role, _now()),
    )
    await _db.commit()
    return uid


async def get_user(user_id: int) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM users WHERE id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_email(email: str) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM users WHERE email=?", (email.lower(),))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_by_google_sub(sub: str) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM users WHERE google_sub=?", (sub,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def link_google_sub(user_id: int, sub: str) -> None:
    await _db.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, user_id))
    await _db.commit()


async def create_session(token_hash: str, user_id: int, expires_at: str,
                         active_org_id: Optional[int] = None) -> None:
    await _db.execute(
        "INSERT INTO sessions (token_hash, user_id, active_org_id, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (token_hash, user_id, active_org_id, _now(), expires_at),
    )
    await _db.commit()


async def get_session_user(token_hash: str) -> Optional[dict]:
    """Return the user for a live session token (with the session's active_org_id)."""
    cur = await _db.execute(
        """SELECT users.*, sessions.active_org_id AS active_org_id
           FROM sessions JOIN users ON sessions.user_id = users.id
           WHERE sessions.token_hash=? AND sessions.expires_at > ?""",
        (token_hash, _now()),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def delete_session(token_hash: str) -> None:
    await _db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
    await _db.commit()


async def set_session_active_org(token_hash: str, org_id: int) -> None:
    """Switch which org a live session is acting in (org switcher). Wired in step 2."""
    await _db.execute(
        "UPDATE sessions SET active_org_id=? WHERE token_hash=?", (org_id, token_hash))
    await _db.commit()


# ---------- teams: memberships & invitations ----------

async def add_membership(user_id: int, org_id: int, role: str = "member") -> None:
    await _db.execute(
        "INSERT OR IGNORE INTO memberships (user_id, org_id, role, created_at) VALUES (?,?,?,?)",
        (user_id, org_id, role, _now()),
    )
    await _db.commit()


async def get_membership(user_id: int, org_id: int) -> Optional[dict]:
    cur = await _db.execute(
        "SELECT * FROM memberships WHERE user_id=? AND org_id=?", (user_id, org_id))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_user_orgs(user_id: int) -> list[dict]:
    """Every org the user belongs to, with their role — drives the org switcher."""
    cur = await _db.execute(
        """SELECT o.id, o.name, m.role FROM memberships m JOIN orgs o ON o.id = m.org_id
           WHERE m.user_id=? ORDER BY o.id""", (user_id,))
    return [dict(r) for r in await cur.fetchall()]


async def list_org_members(org_id: int) -> list[dict]:
    cur = await _db.execute(
        """SELECT u.id, u.email, u.name, m.role, m.created_at FROM memberships m
           JOIN users u ON u.id = m.user_id WHERE m.org_id=? ORDER BY m.created_at""",
        (org_id,))
    return [dict(r) for r in await cur.fetchall()]


async def set_membership_role(user_id: int, org_id: int, role: str) -> None:
    await _db.execute(
        "UPDATE memberships SET role=? WHERE user_id=? AND org_id=?", (role, user_id, org_id))
    await _db.commit()


async def remove_membership(user_id: int, org_id: int) -> None:
    await _db.execute(
        "DELETE FROM memberships WHERE user_id=? AND org_id=?", (user_id, org_id))
    await _db.commit()


async def count_org_admins(org_id: int) -> int:
    cur = await _db.execute(
        "SELECT COUNT(*) FROM memberships WHERE org_id=? AND role='admin'", (org_id,))
    return (await cur.fetchone())[0]


async def create_invitation(org_id: int, email: str, role: str, token: str,
                            invited_by: int, expires_at: str,
                            new_workspace: int = 0) -> int:
    """Create a pending invitation. new_workspace=0 => the invitee joins `org_id`
    (a teammate invite). new_workspace=1 => a signup invite: `org_id` is only the
    'created by' reference; on register the invitee gets their OWN fresh org."""
    cur = await _db.execute(
        """INSERT INTO invitations
           (org_id, email, role, token, invited_by, status, created_at, expires_at,
            new_workspace)
           VALUES (?,?,?,?,?, 'pending', ?, ?, ?)""",
        (org_id, email.lower(), role, token, invited_by, _now(), expires_at,
         int(new_workspace)),
    )
    await _db.commit()
    return cur.lastrowid


async def list_signup_invitations(status: str = "pending") -> list[dict]:
    """Own-org signup invites (new_workspace=1) across the instance — for the
    operator's onboarding view. Not scoped to an org."""
    cur = await _db.execute(
        "SELECT * FROM invitations WHERE new_workspace=1 AND status=? "
        "ORDER BY id DESC", (status,))
    return [dict(r) for r in await cur.fetchall()]


async def get_invitation(inv_id: int) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM invitations WHERE id=?", (inv_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_invitation_by_token(token: str) -> Optional[dict]:
    cur = await _db.execute("SELECT * FROM invitations WHERE token=?", (token,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_pending_invitation_for_email(email: str) -> Optional[dict]:
    """Newest pending, non-expired invitation for this email — the gate for
    invite-only signup."""
    cur = await _db.execute(
        "SELECT * FROM invitations WHERE lower(email)=lower(?) AND status='pending' "
        "AND expires_at > ? ORDER BY id DESC LIMIT 1", (email, _now()))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_org_invitations(org_id: int, status: Optional[str] = None) -> list[dict]:
    if status:
        cur = await _db.execute(
            "SELECT * FROM invitations WHERE org_id=? AND status=? ORDER BY id DESC",
            (org_id, status))
    else:
        cur = await _db.execute(
            "SELECT * FROM invitations WHERE org_id=? ORDER BY id DESC", (org_id,))
    return [dict(r) for r in await cur.fetchall()]


async def set_invitation_status(invite_id: int, status: str,
                                accepted_at: Optional[str] = None) -> None:
    await _db.execute(
        "UPDATE invitations SET status=?, accepted_at=? WHERE id=?",
        (status, accepted_at, invite_id))
    await _db.commit()


# ---------- connections (SaaS integrations) ----------

async def upsert_connection(user_id: int, provider: str, *, org_id: Optional[int] = None,
                            status: str, external_ref: Optional[str] = None) -> None:
    await _db.execute(
        """INSERT INTO connections (user_id, org_id, provider, external_ref, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id, provider) DO UPDATE SET
             status=excluded.status,
             external_ref=COALESCE(excluded.external_ref, connections.external_ref),
             updated_at=excluded.updated_at""",
        (user_id, org_id, provider, external_ref, status, _now(), _now()),
    )
    await _db.commit()


async def set_connection_status(user_id: int, provider: str, status: str) -> None:
    await _db.execute(
        "UPDATE connections SET status=?, updated_at=? WHERE user_id=? AND provider=?",
        (status, _now(), user_id, provider),
    )
    await _db.commit()


async def get_connection(user_id: int, provider: str) -> Optional[dict]:
    cur = await _db.execute(
        "SELECT * FROM connections WHERE user_id=? AND provider=?", (user_id, provider)
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_connections(user_id: int) -> list[dict]:
    cur = await _db.execute(
        "SELECT * FROM connections WHERE user_id=? ORDER BY provider", (user_id,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def active_providers(user_id: int) -> list[str]:
    cur = await _db.execute(
        "SELECT provider FROM connections WHERE user_id=? AND status='active'", (user_id,)
    )
    return [r["provider"] for r in await cur.fetchall()]


async def delete_connection(user_id: int, provider: str) -> None:
    await _db.execute(
        "DELETE FROM connections WHERE user_id=? AND provider=?", (user_id, provider)
    )
    await _db.commit()

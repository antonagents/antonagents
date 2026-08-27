"""FastAPI service: create agents, run them (chat, one-shot, or scheduled), stream progress.

An **agent** is a persistent, reusable configuration; each execution is a **run**
(a chat turn, a manual trigger, or a scheduled fire). Everything is scoped to an
**org** (team).

Endpoints (representative)
    GET  /  ·  GET /app             -> landing page · web console (SPA)
    POST /api/auth/register         -> create account (email/password)
    POST /api/auth/login | logout   -> log in / out
    GET  /api/auth/me               -> current user (401 if not logged in)
    GET  /api/auth/google/login     -> begin Google OAuth (if configured)
    GET  /api/health                -> health + config summary
    POST /api/agents                -> create an agent (+ run_now / schedule)
    GET  /api/agents                -> list the org's agents
    GET  /api/agents/{id}           -> agent detail + run history
    POST /api/agents/{id}/run       -> trigger a run now
    POST /api/agents/{id}/chat      -> chat turn: a run resuming the session
    POST /api/agents/{id}/enable    -> pause/resume a schedule
    DELETE /api/agents/{id}         -> delete an agent + its workspace
    GET  /api/agents/{id}/files     -> browse artifacts (.../file -> view/download)
    GET/POST /api/db-connections    -> read-only DB data sources (org-scoped)
    GET  /api/runs/{id}             -> run detail + all stored events
    GET  /api/runs/{id}/stream      -> live SSE event stream
    (also: /api/connectors*, /api/skills*, /api/org* — teams & invitations)

All API endpoints require a session and are scoped to the caller's active org —
a resource outside that org returns 404.
"""
import asyncio
import json
import mimetypes
import os
import re
import secrets
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, connectors, db, providers, ratelimit, runner, scheduler, templates
from .auth import current_user
from .connectors import db_driver
from .models import (AgentAlertsIn, AgentConnectorsIn, AgentCreate, AgentModelIn, AgentNameIn,
                     AgentDbConnectionsIn, AgentScheduleIn, AgentSkillsIn, ApiKeyIn,
                     DbConnectionIn, DbConnectionTestIn, DbConnectionUpdateIn,
                     InvitationIn, LoginIn, MemberRoleIn, SignupInviteIn,
                     NotificationsReadIn, OrgSwitchIn, RegisterIn,
                     ReplyIn, SkillIn, V1ChatIn)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


log = logging.getLogger("superagent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    providers.load()
    await db.init()
    runner.init()
    await scheduler.start()
    if config.CONNECTORS_ENABLED:
        # The per-user MCP config injected into task containers currently carries
        # the org-wide connector key — a task with shell+network could exfiltrate
        # it. Safe only when all users are trusted. See docs/SECURITY.md.
        log.warning(
            "Connectors enabled (%s): the provider key is injected into task "
            "containers — enable only for trusted users. See docs/SECURITY.md.",
            config.CONNECTORS_PROVIDER,
        )
    yield
    scheduler.shutdown()
    await db.close()


app = FastAPI(title="Antonagents", lifespan=lifespan)
# Session middleware backs the OAuth `state` handshake (authlib stores it here).
app.add_middleware(
    SessionMiddleware, secret_key=config.SESSION_SECRET,
    same_site="lax", https_only=config.COOKIE_SECURE,
)
# self-hosted fonts (no external CDN — matches the self-host / data-residency story)
app.mount("/fonts", StaticFiles(directory=str(WEB_DIR / "fonts")), name="fonts")


# ---------------- auth ----------------

@app.get("/api/auth/config")
async def auth_config():
    """Public: lets the login UI decide which buttons to show."""
    return {"google_enabled": config.GOOGLE_ENABLED, "password_enabled": True,
            "signup_open": config.SIGNUP_OPEN}


def _user_out(u: dict) -> dict:
    out = {"id": u["id"], "email": u["email"], "name": u.get("name")}
    # present when resolved by current_user (teams model): active org, role, memberships
    if u.get("active_org_id") is not None:
        out["active_org_id"] = u["active_org_id"]
        out["role"] = u.get("active_role")
        out["orgs"] = u.get("orgs") or []
        out["is_operator"] = _is_operator(u)
    return out


async def _resolve_signup(email: str, name: str | None) -> tuple[int, str, dict | None]:
    """For a brand-new user, enforce the signup mode and return (org_id, role,
    invitation). 'open' -> a fresh personal org (admin, no invite). 'invite' ->
    requires a pending invitation; the user joins the inviter's org with the invited
    role, and the invitation is returned so the caller can mark it accepted."""
    if config.SIGNUP_OPEN:
        label = name or email.split("@")[0]
        return await db.create_org(f"{label}'s workspace"), "admin", None
    inv = await db.get_pending_invitation_for_email(email)
    if not inv:
        raise HTTPException(403, "Sign-ups are invite-only on this instance. "
                                 "Ask an admin to invite your email, or request access.")
    if inv.get("new_workspace"):
        # signup invite: the invitee gets their OWN fresh workspace (admin), not
        # the inviter's org. inv["org_id"] is only the created-by reference.
        label = name or email.split("@")[0]
        return await db.create_org(f"{label}'s workspace"), "admin", inv
    return inv["org_id"], inv["role"], inv


@app.post("/api/auth/register")
async def register(body: RegisterIn, response: Response):
    if await db.get_user_by_email(body.email):
        raise HTTPException(409, "email already registered")
    org_id, role, inv = await _resolve_signup(body.email, body.name)
    uid = await db.create_user(
        body.email, org_id=org_id, name=body.name,
        password_hash=auth.hash_password(body.password), role=role,
    )
    if inv:
        await db.set_invitation_status(inv["id"], "accepted", _now_iso())
    auth.set_session_cookie(response, await auth.new_session(uid))
    return _user_out(await db.get_user(uid))


@app.post("/api/auth/login")
async def login(body: LoginIn, response: Response):
    u = await db.get_user_by_email(body.email)
    if not u or not auth.verify_password(body.password, u.get("password_hash")):
        raise HTTPException(401, "invalid email or password")
    auth.set_session_cookie(response, await auth.new_session(u["id"]))
    return _user_out(u)


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    await auth.logout(request)
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: dict = Depends(current_user)):
    return _user_out(user)


@app.get("/api/auth/google/login")
async def google_login(request: Request):
    if not config.GOOGLE_ENABLED:
        raise HTTPException(404, "google login not configured")
    return await auth.oauth().google.authorize_redirect(request, config.GOOGLE_REDIRECT_URI)


@app.get("/api/auth/google/callback")
async def google_callback(request: Request):
    if not config.GOOGLE_ENABLED:
        raise HTTPException(404, "google login not configured")
    try:
        token = await auth.oauth().google.authorize_access_token(request)
    except Exception as e:  # noqa: BLE001 - surface OAuth failures as a clean redirect
        return RedirectResponse(f"/app?auth_error={type(e).__name__}")
    info = token.get("userinfo") or {}
    sub, email = info.get("sub"), (info.get("email") or "").lower()
    if not sub or not email:
        return RedirectResponse("/app?auth_error=no_email")

    user = await db.get_user_by_google_sub(sub)
    if not user:
        # link to an existing email account, or create a new one
        user = await db.get_user_by_email(email)
        if user:
            await db.link_google_sub(user["id"], sub)
        else:
            # brand-new user via Google — same signup gating as password register
            if config.SIGNUP_OPEN:
                label = info.get("name") or email.split("@")[0]
                org_id, role, inv = await db.create_org(f"{label}'s workspace"), "admin", None
            else:
                inv = await db.get_pending_invitation_for_email(email)
                if not inv:
                    return RedirectResponse("/app?auth_error=invite_only")
                org_id, role = inv["org_id"], inv["role"]
            uid = await db.create_user(email, org_id=org_id, name=info.get("name"),
                                       google_sub=sub, role=role)
            if inv:
                await db.set_invitation_status(inv["id"], "accepted", _now_iso())
            user = await db.get_user(uid)

    resp = RedirectResponse("/app")
    auth.set_session_cookie(resp, await auth.new_session(user["id"]))
    return resp


# ---------------- service info ----------------

@app.get("/api/health")
async def health(user: dict = Depends(current_user)):
    pubs = providers.to_public()
    return {
        "ok": True,
        "task_image": config.TASK_IMAGE,
        "max_concurrent": config.MAX_CONCURRENT,
        "default_provider": config.DEFAULT_PROVIDER,
        "providers_configured": [p["name"] for p in pubs if p["configured"]],
        "any_provider_configured": any(p["configured"] for p in pubs),
    }


@app.get("/api/providers")
async def list_providers(user: dict = Depends(current_user)):
    return await providers.to_public_dynamic()


# ---------------- connectors (SaaS integrations) ----------------

def _connector_provider_or_404(provider: str):
    conn = connectors.get_connector()
    if not config.CONNECTORS_ENABLED:
        raise HTTPException(404, "connectors are not configured")
    if provider not in {p["slug"] for p in conn.providers()}:
        raise HTTPException(404, f"unknown connector '{provider}'")
    return conn


@app.get("/api/connectors")
async def list_connectors(user: dict = Depends(current_user)):
    """Available connectors merged with the caller's connection status."""
    conn = connectors.get_connector()
    rows = {c["provider"]: c["status"] for c in await db.list_connections(user["id"])}
    return {
        "enabled": config.CONNECTORS_ENABLED,
        "connectors": [
            {"slug": p["slug"], "label": p["label"],
             "category": p.get("category", "Other"),
             "status": rows.get(p["slug"], "none")}
            for p in conn.providers()
        ],
    }


@app.post("/api/connectors/{provider}/connect")
async def connect_connector(provider: str, user: dict = Depends(current_user)):
    conn = _connector_provider_or_404(provider)
    try:
        res = await asyncio.to_thread(conn.initiate, user["id"], provider)
    except RuntimeError as e:  # e.g. a toolkit that needs a bring-your-own app
        raise HTTPException(400, str(e))
    if not res.get("redirect_url"):
        raise HTTPException(502, "connector did not return an authorization URL")
    await db.upsert_connection(user["id"], provider, org_id=user.get("org_id"),
                               status="pending", external_ref=res.get("ref"))
    return {"redirect_url": res["redirect_url"]}


@app.get("/api/connectors/{provider}/status")
async def connector_status(provider: str, user: dict = Depends(current_user)):
    conn = _connector_provider_or_404(provider)
    status = await asyncio.to_thread(conn.status, user["id"], provider)
    if status == "none":
        await db.delete_connection(user["id"], provider)
    else:
        await db.set_connection_status(user["id"], provider, status)
    return {"provider": provider, "status": status}


@app.delete("/api/connectors/{provider}")
async def disconnect_connector(provider: str, user: dict = Depends(current_user)):
    conn = _connector_provider_or_404(provider)
    await asyncio.to_thread(conn.disconnect, user["id"], provider)
    await db.delete_connection(user["id"], provider)
    return {"disconnected": provider}


# ---------------- agents ----------------

async def _agent_out(agent: dict) -> dict:
    return _agent_out_row(agent, await db.last_run(agent["id"]))


async def _agents_out(agents: list[dict]) -> list[dict]:
    """Bulk version of _agent_out — one query for all last-runs (no N+1)."""
    lasts = await db.last_runs_for([a["id"] for a in agents])
    return [_agent_out_row(a, lasts.get(a["id"])) for a in agents]


def _agent_out_row(agent: dict, last: dict | None) -> dict:
    return {
        "id": agent["id"],
        "public_id": agent.get("public_id"),
        "name": agent["name"],
        "prompt": agent["prompt"],
        "schedule_kind": agent["schedule_kind"],
        "schedule_expr": agent.get("schedule_expr"),
        "enabled": agent["enabled"],
        "provider": agent.get("provider"),
        "model": agent.get("model"),
        "created_at": agent["created_at"],
        "kind": agent.get("kind") or "routine",
        "next_run_at": scheduler.next_run_at(agent["id"]),
        "last_run": last,
        "can_reply": bool(agent.get("last_session_id")),
    }


def _org_id(user: dict) -> int:
    """The org the caller is currently acting in (teams model), home org as fallback."""
    return user.get("active_org_id") or user["org_id"]


async def require_admin(user: dict = Depends(current_user)) -> dict:
    """Dependency for org-management endpoints: caller must be an admin of the
    active org. Used by the member/invitation endpoints (step 4)."""
    if user.get("active_role") != "admin":
        raise HTTPException(403, "admin role required")
    return user


def _iso_days_ago(days: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - max(0, days) * 86400))


def _classify_activity(row: dict) -> dict:
    """Turn a raw tool_use event into a governance-readable activity record:
    category (shell/web/files/app/task/skill) + a human action + its target."""
    try:
        d = json.loads(row["data"]) if isinstance(row["data"], str) else (row["data"] or {})
    except Exception:
        d = {}
    name = d.get("name") or "?"
    inp = d.get("input") if isinstance(d.get("input"), dict) else {}
    cat, action, target = "other", name, ""
    if name == "Bash":
        cmd = inp.get("command") or ""
        m = re.search(r"SUPERAGENT_DB_(\w+)", cmd)   # our data-source DSNs are injected as env vars
        if m or re.match(r"^\s*(psql|mysql|mongosh|sqlite3|mongo)\b", cmd):
            cat, action = "data", "query data source"
            target = (m.group(1).lower() if m else cmd.split()[0]) if cmd.split() else "db"
        else:
            cat, action, target = "shell", "run command", cmd[:160]
    elif name == "WebSearch":
        cat, action, target = "web", "search", (inp.get("query") or "")[:160]
    elif name == "WebFetch":
        cat, action, target = "web", "fetch", (inp.get("url") or "")[:160]
    elif name in ("Read", "Write", "Edit"):
        cat, action, target = "files", name.lower(), (inp.get("file_path") or "")[:160]
    elif name in ("Agent", "Task"):
        cat, action, target = "task", "subagent", (inp.get("description") or "")[:120]
    elif name in ("TaskCreate", "TaskUpdate"):
        cat, action, target = "task", name, ""
    elif name == "Skill":
        cat, action, target = "skill", "use skill", (inp.get("command") or inp.get("skill") or "")[:120]
    elif name.startswith("mcp__"):
        cat = "app"
        segs = name.split("__")            # ['mcp','composio_googledrive','GOOGLEDRIVE_DOWNLOAD_FILE']
        app, act = "app", name
        if len(segs) >= 3:
            prov = segs[1].split("_")
            app = prov[-1] if prov else segs[1]
            act = segs[2]
            if act.upper().startswith(app.upper() + "_"):
                act = act[len(app) + 1:]   # GOOGLEDRIVE_DOWNLOAD_FILE -> DOWNLOAD_FILE
        action, target = act, app
    return {"id": row["id"], "ts": row["ts"], "run_id": row["run_id"],
            "agent_id": row["agent_id"], "agent_name": row["agent_name"],
            "category": cat, "action": action, "target": target}


@app.get("/api/observability/health")
async def observability_health(days: int = 30, user: dict = Depends(require_admin)):
    """Org-wide run health (governance/oversight) — admin only."""
    return await db.ops_health(_org_id(user), _iso_days_ago(days))


@app.get("/api/observability/activity")
async def observability_activity(days: int = 30, agent_id: int | None = None,
                                 limit: int = 100, offset: int = 0,
                                 user: dict = Depends(require_admin)):
    """Audit feed: what agents did (classified tool activity) — admin only."""
    cap = min(max(1, limit), 500)
    rows = await db.activity(_org_id(user), _iso_days_ago(days), agent_id, cap, max(0, offset))
    return {"activity": [_classify_activity(r) for r in rows],
            "next_offset": (offset + cap) if len(rows) == cap else None}


def _is_operator(user: dict) -> bool:
    """An instance operator may onboard *new teams* (own-org signup links).
    If SUPERAGENT_OPERATORS is set, only those emails qualify; otherwise any org
    admin does (convenient for self-hosting)."""
    ops = config.OPERATOR_EMAILS
    if ops:
        return (user.get("email") or "").lower() in ops
    return user.get("active_role") == "admin"


async def require_operator(user: dict = Depends(current_user)) -> dict:
    if not _is_operator(user):
        raise HTTPException(403, "operator access required")
    return user


@app.post("/api/agents")
async def create_agent(body: AgentCreate, user: dict = Depends(current_user)):
    if body.schedule_kind in ("cron", "interval") and not body.schedule_expr:
        raise HTTPException(400, "schedule_expr is required for cron/interval agents")

    agent_id = await db.create_agent(body.model_dump(), org_id=_org_id(user), owner_id=user["id"])
    agent = await db.get_agent(agent_id)

    if body.schedule_kind in ("cron", "interval"):
        try:
            scheduler.add_agent(agent)
        except Exception as e:
            await db.delete_agent(agent_id)
            raise HTTPException(400, f"invalid schedule: {e}")

    if body.run_now:
        run_id = await db.create_run(agent_id, prompt=agent["prompt"])
        await runner.start_run(run_id, agent)

    return await _agent_out(agent)


@app.get("/api/agents")
async def list_agents(user: dict = Depends(current_user)):
    # the Routines list — chat threads are excluded (see /api/chats)
    return await _agents_out(await db.list_agents(_org_id(user), kind="routine"))


# ---------------- chat threads ----------------

@app.get("/api/chats")
async def list_chats(user: dict = Depends(current_user)):
    return await _agents_out(await db.list_agents(_org_id(user), kind="chat"))


@app.post("/api/chats")
async def create_chat(user: dict = Depends(current_user)):
    """Start a new interactive chat thread (a chat-kind agent with no schedule).
    It can use every tool the user has connected."""
    agent_id = await db.create_agent(
        {"name": "New chat", "kind": "chat", "prompt": ""},
        org_id=_org_id(user), owner_id=user["id"],
    )
    return await _agent_out(await db.get_agent(agent_id))


async def _owned_agent_or_404(agent_id: int, user: dict) -> dict:
    agent = await db.get_owned_agent(agent_id, _org_id(user))
    if not agent:
        raise HTTPException(404, "agent not found")
    return agent


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: int, user: dict = Depends(current_user)):
    agent = await _owned_agent_or_404(agent_id, user)
    out = await _agent_out(agent)
    out["runs"] = await db.list_runs(agent_id)
    out["connectors"] = await db.get_agent_connectors(agent_id)
    out["skills"] = await db.get_agent_skills(agent_id)
    out["db_connections"] = [
        {"id": c["id"], "name": c["name"], "engine": c["engine"], "mode": c["mode"]}
        for c in await db.get_agent_db_connections(agent_id)
    ]
    out["baseline_minutes"] = agent.get("baseline_minutes")
    out["impact"] = _with_cost(await db.agent_impact(agent_id, agent.get("baseline_minutes")))
    out["alerts"] = await db.get_agent_alerts(agent_id)
    return out


def _with_cost(impact: dict) -> dict:
    """Add dollars-saved derived from minutes saved and the configured hourly rate."""
    impact["cost_saved_usd"] = round(impact["minutes_saved"] / 60 * config.HOURLY_RATE, 2)
    return impact


@app.get("/api/impact")
async def org_impact(user: dict = Depends(current_user)):
    """Org-wide impact rollup for the console's impact strip."""
    data = _with_cost(await db.org_impact(_org_id(user)))
    data["hourly_rate"] = config.HOURLY_RATE
    return data


@app.put("/api/agents/{agent_id}/connectors")
async def set_agent_connectors(agent_id: int, body: AgentConnectorsIn,
                               user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    valid = {p["slug"] for p in connectors.get_connector().providers()}
    await db.set_agent_connectors(agent_id, [p for p in body.providers if p in valid])
    return {"connectors": await db.get_agent_connectors(agent_id)}


@app.put("/api/agents/{agent_id}/skills")
async def set_agent_skills(agent_id: int, body: AgentSkillsIn,
                           user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    org_skill_ids = {s["id"] for s in await db.list_skills(_org_id(user))}
    await db.set_agent_skills(agent_id, [s for s in body.skill_ids if s in org_skill_ids])
    return {"skills": [s["id"] for s in await db.get_agent_skills(agent_id)]}


# ---------------- skills (org-scoped library) ----------------

@app.get("/api/skills")
async def list_skills(user: dict = Depends(current_user)):
    return await db.list_skills(_org_id(user))


@app.post("/api/skills")
async def create_skill(body: SkillIn, user: dict = Depends(current_user)):
    sid = await db.create_skill(_org_id(user), body.name,
                                description=body.description, instructions=body.instructions)
    return {"id": sid, "name": body.name}


# ---------------- database connections (native connectors) ----------------

@app.get("/api/db-engines")
async def db_engines(user: dict = Depends(current_user)):
    return db_driver.engines()


@app.get("/api/db-connections")
async def list_db_connections(user: dict = Depends(current_user)):
    return await db.list_db_connections(_org_id(user))


@app.post("/api/db-connections")
async def create_db_connection(body: DbConnectionIn, user: dict = Depends(current_user)):
    encrypted = db_driver.encrypt(body.dsn)  # DSN never stored in plaintext
    try:
        cid = await db.create_db_connection(
            _org_id(user), user["id"], name=body.name, engine=body.engine,
            dsn_encrypted=encrypted, mode=body.mode)
    except Exception as e:  # UNIQUE(org_id, name) or similar
        raise HTTPException(409, f"could not create connection: {type(e).__name__}")
    return {"id": cid, "name": body.name, "engine": body.engine, "mode": body.mode}


@app.post("/api/db-connections/test")
async def test_db_connection_raw(body: DbConnectionTestIn, user: dict = Depends(current_user)):
    """Test a connection string before saving it (from the connect form)."""
    return await runner.test_db_connection(body.engine, body.dsn, body.mode)


@app.post("/api/db-connections/{conn_id}/test")
async def test_db_connection_existing(conn_id: int, user: dict = Depends(current_user)):
    """Test a saved data source's stored credentials."""
    c = await db.get_db_connection(conn_id, _org_id(user))
    if not c:
        raise HTTPException(404, "data source not found")
    return await runner.test_db_connection(c["engine"], db_driver.decrypt(c["dsn_encrypted"]), c["mode"])


@app.put("/api/db-connections/{conn_id}")
async def edit_db_connection(conn_id: int, body: DbConnectionUpdateIn,
                             user: dict = Depends(current_user)):
    """Update a data source — rotate credentials (dsn), rename, or change access mode."""
    c = await db.get_db_connection(conn_id, _org_id(user))
    if not c:
        raise HTTPException(404, "data source not found")
    enc = db_driver.encrypt(body.dsn) if body.dsn else None  # DSN never stored in plaintext
    await db.update_db_connection(conn_id, _org_id(user), name=body.name,
                                  dsn_encrypted=enc, mode=body.mode)
    updated = await db.get_db_connection(conn_id, _org_id(user))
    return {"id": conn_id, "name": updated["name"], "engine": updated["engine"], "mode": updated["mode"]}


@app.delete("/api/db-connections/{conn_id}")
async def delete_db_connection(conn_id: int, user: dict = Depends(current_user)):
    await db.delete_db_connection(conn_id, _org_id(user))
    return {"deleted": conn_id}


@app.put("/api/agents/{agent_id}/db-connections")
async def set_agent_db_connections(agent_id: int, body: AgentDbConnectionsIn,
                                   user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    org_ids = {c["id"] for c in await db.list_db_connections(_org_id(user))}
    await db.set_agent_db_connections(agent_id, [i for i in body.db_connection_ids if i in org_ids])
    return {"db_connections": [c["id"] for c in await db.get_agent_db_connections(agent_id)]}


# ---------------- teams: orgs, members, invitations ----------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _invite_expiry_iso(days: int = 14) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400))


@app.get("/api/orgs")
async def my_orgs(user: dict = Depends(current_user)):
    """Every org the caller belongs to (drives the org switcher)."""
    return {"active_org_id": _org_id(user), "orgs": await db.list_user_orgs(user["id"])}


@app.get("/api/org")
async def current_org(user: dict = Depends(current_user)):
    oid = _org_id(user)
    org = await db.get_org(oid)
    members = await db.list_org_members(oid)
    return {"id": oid, "name": (org or {}).get("name"),
            "role": user.get("active_role"), "member_count": len(members)}


@app.post("/api/org/switch")
async def switch_org(body: OrgSwitchIn, request: Request, user: dict = Depends(current_user)):
    if not await db.get_membership(user["id"], body.org_id):
        raise HTTPException(403, "not a member of that org")
    th = auth.session_token_hash(request)
    if th:
        await db.set_session_active_org(th, body.org_id)
    return {"active_org_id": body.org_id}


@app.get("/api/org/members")
async def org_members(user: dict = Depends(current_user)):
    return await db.list_org_members(_org_id(user))


@app.delete("/api/org/members/{uid}")
async def remove_member(uid: int, user: dict = Depends(current_user)):
    """Leave the org (uid == self) or, for admins, remove another member."""
    oid = _org_id(user)
    if uid != user["id"] and user.get("active_role") != "admin":
        raise HTTPException(403, "admin role required")
    target = await db.get_membership(uid, oid)
    if not target:
        raise HTTPException(404, "not a member of this org")
    if target["role"] == "admin" and await db.count_org_admins(oid) <= 1:
        raise HTTPException(400, "can't remove the last admin")
    await db.remove_membership(uid, oid)
    return {"removed": uid}


@app.patch("/api/org/members/{uid}")
async def set_member_role(uid: int, body: MemberRoleIn, user: dict = Depends(require_admin)):
    oid = _org_id(user)
    target = await db.get_membership(uid, oid)
    if not target:
        raise HTTPException(404, "not a member of this org")
    if (target["role"] == "admin" and body.role != "admin"
            and await db.count_org_admins(oid) <= 1):
        raise HTTPException(400, "can't demote the last admin")
    await db.set_membership_role(uid, oid, body.role)
    return {"user_id": uid, "role": body.role}


@app.get("/api/org/invitations")
async def list_invitations(user: dict = Depends(require_admin)):
    return await db.list_org_invitations(_org_id(user), status="pending")


@app.post("/api/org/invitations")
async def create_invitation(body: InvitationIn, user: dict = Depends(require_admin)):
    token = secrets.token_urlsafe(24)
    inv_id = await db.create_invitation(
        _org_id(user), body.email, body.role, token, user["id"], _invite_expiry_iso())
    return {"id": inv_id, "email": body.email.lower(), "role": body.role,
            "token": token, "path": f"/invite/{token}"}


@app.delete("/api/org/invitations/{inv_id}")
async def revoke_invitation(inv_id: int, user: dict = Depends(require_admin)):
    ids = {i["id"] for i in await db.list_org_invitations(_org_id(user))}
    if inv_id not in ids:
        raise HTTPException(404, "invitation not found")
    await db.set_invitation_status(inv_id, "revoked")
    return {"revoked": inv_id}


# --- signup invites: onboard a NEW team into its own fresh workspace ----------

def _base_url(request: Request) -> str:
    """Public origin, honoring the reverse proxy (Caddy) so generated links use
    the real https host rather than the internal bind address."""
    h = request.headers
    proto = h.get("x-forwarded-proto", request.url.scheme)
    host = h.get("x-forwarded-host") or h.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _signup_invite_out(request: Request, inv: dict) -> dict:
    token = inv["token"]
    return {
        "id": inv["id"], "email": inv["email"], "status": inv["status"],
        "created_at": inv.get("created_at"), "expires_at": inv.get("expires_at"),
        "token": token, "path": f"/signup/{token}",
        "url": f"{_base_url(request)}/signup/{token}",
    }


@app.get("/api/signup-invites")
async def list_signup_invites(request: Request, user: dict = Depends(require_operator)):
    invs = await db.list_signup_invitations(status="pending")
    return [_signup_invite_out(request, i) for i in invs]


@app.post("/api/signup-invites")
async def create_signup_invite(body: SignupInviteIn, request: Request,
                               user: dict = Depends(require_operator)):
    if await db.get_user_by_email(body.email):
        raise HTTPException(409, "a user with that email already exists")
    token = secrets.token_urlsafe(24)
    inv_id = await db.create_invitation(
        _org_id(user), body.email, "admin", token, user["id"],
        _invite_expiry_iso(), new_workspace=1)
    return _signup_invite_out(request, await db.get_invitation(inv_id))


@app.delete("/api/signup-invites/{inv_id}")
async def revoke_signup_invite(inv_id: int, user: dict = Depends(require_operator)):
    inv = await db.get_invitation(inv_id)
    if not inv or not inv.get("new_workspace"):
        raise HTTPException(404, "signup invite not found")
    await db.set_invitation_status(inv_id, "revoked")
    return {"revoked": inv_id}


@app.get("/api/invitations/{token}")
async def preview_invitation(token: str):
    """Public preview of an invite (given its token) — for the accept page."""
    inv = await db.get_invitation_by_token(token)
    if not inv:
        raise HTTPException(404, "invitation not found")
    org = await db.get_org(inv["org_id"])
    return {"email": inv["email"], "role": inv["role"], "status": inv["status"],
            "org_name": (org or {}).get("name"),
            "new_workspace": bool(inv.get("new_workspace")),
            "expired": inv["expires_at"] <= _now_iso()}


@app.post("/api/invitations/{token}/accept")
async def accept_invitation(token: str, request: Request, user: dict = Depends(current_user)):
    inv = await db.get_invitation_by_token(token)
    if not inv or inv["status"] != "pending":
        raise HTTPException(404, "invitation not found or already used")
    if inv["expires_at"] <= _now_iso():
        raise HTTPException(400, "invitation has expired")
    if (user.get("email") or "").lower() != (inv["email"] or "").lower():
        raise HTTPException(403, f"this invitation is for {inv['email']}; sign in as that user to accept")
    await db.add_membership(user["id"], inv["org_id"], inv["role"])
    await db.set_invitation_status(inv["id"], "accepted", _now_iso())
    th = auth.session_token_hash(request)   # land them in the org they just joined
    if th:
        await db.set_session_active_org(th, inv["org_id"])
    return {"org_id": inv["org_id"], "role": inv["role"]}


# ---------------- artifacts: browse / view / download the agent's deliverables ----------------
# Agents are told (runner._ARTIFACTS_NOTE) to write FINISHED deliverables into
# ./artifacts/ under their /workspace volume, keeping scratch scripts/logs/downloads
# out of it. We surface only that subdir, so the user sees deliverables — not the
# agent's working files. Reads go through the Docker daemon (runner.list_artifacts /
# read_artifact) so they work on every OS; every path is validated to stay inside
# artifacts/ before it reaches the helper container.
_ARTIFACT_MAX_FILES = 500
_ARTIFACTS_SUBDIR = "artifacts"


_WS_SERVE_DIRS = {"artifacts": _ARTIFACTS_SUBDIR, "uploads": "uploads"}


def _safe_ws_relpath(rel: str, subdir: str = "artifacts") -> str:
    """Validate `rel` stays within the allowed workspace subdir (artifacts/ or
    uploads/) and return the in-container path /ws/<subdir>/<rel>. Rejects
    absolute paths, parent-directory escapes, and unknown subdirs."""
    root = _WS_SERVE_DIRS.get(subdir)
    if root is None:
        raise HTTPException(400, "invalid dir")
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part == ".." for part in p.parts):
        raise HTTPException(400, "invalid path")
    return str(PurePosixPath("/ws") / root / p)


@app.get("/api/agents/{agent_id}/files")
async def list_agent_files(agent_id: int, user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    rows = await runner.list_artifacts(agent_id)
    if rows is None:                       # no workspace volume yet (never ran)
        return {"files": [], "truncated": False}
    rows.sort(key=lambda r: r.get("mtime", 0.0), reverse=True)
    truncated = len(rows) > _ARTIFACT_MAX_FILES
    files = [{
        "path": r["path"],
        "size": r["size"],
        "modified": datetime.fromtimestamp(r["mtime"], timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
    } for r in rows[:_ARTIFACT_MAX_FILES]]
    return {"files": files, "truncated": truncated}


@app.get("/api/agents/{agent_id}/file")
async def get_agent_file(agent_id: int, path: str = Query(..., min_length=1),
                         dir: str = "artifacts", dl: int = 0,
                         user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    ws_path = _safe_ws_relpath(path, dir)
    data = await runner.read_artifact(agent_id, ws_path)
    if data is None:
        raise HTTPException(404, "file not found")
    media, _ = mimetypes.guess_type(path)
    fname = PurePosixPath(path).name.replace('"', "")
    # Artifacts are agent-generated and served from our own origin, so an HTML/SVG
    # deliverable could otherwise run scripts with the user's session. `sandbox`
    # (no tokens) disables JS and gives the doc a unique origin while CSS/images
    # still render; nosniff prevents MIME confusion. Safe to view, correct to look at.
    return Response(
        content=data,
        media_type=media or "application/octet-stream",
        headers={
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'{"attachment" if dl else "inline"}; filename="{fname}"',
        },
    )


def _safe_upload_name(raw: str) -> str:
    """Reduce an uploaded filename to a safe basename inside uploads/: strip any
    path, drop leading dots, allowlist chars, cap length. Never returns '' or a
    name that could escape the directory."""
    base = PurePosixPath((raw or "").replace("\\", "/")).name   # drop any path
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).lstrip(".")    # allowlist + no dotfiles
    base = base[:120]
    return base or "upload"


@app.post("/api/agents/{agent_id}/upload")
async def upload_agent_file(agent_id: int, file: UploadFile = File(...),
                            user: dict = Depends(current_user)):
    """Upload a file into a chat/agent's workspace (uploads/). The next turn's
    agent reads it from ./uploads/."""
    await _owned_agent_or_404(agent_id, user)
    cap = config.UPLOAD_MAX_MB * 1024 * 1024
    data = await file.read(cap + 1)
    if len(data) > cap:
        raise HTTPException(413, f"file too large (max {config.UPLOAD_MAX_MB} MB)")
    if not data:
        raise HTTPException(400, "empty file")
    name = _safe_upload_name(file.filename or "upload")
    try:
        path = await runner.write_upload(agent_id, name, data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"could not store upload: {type(e).__name__}")
    return {"name": name, "path": path, "size": len(data)}


@app.post("/api/agents/{agent_id}/run")
async def run_agent_now(agent_id: int, user: dict = Depends(current_user)):
    agent = await _owned_agent_or_404(agent_id, user)
    run_id = await db.create_run(agent_id, prompt=agent["prompt"])
    await runner.start_run(run_id, agent)
    return {"run_id": run_id}


@app.post("/api/agents/{agent_id}/chat")
async def chat_agent(agent_id: int, body: ReplyIn, user: dict = Depends(current_user)):
    """A chat turn: a run whose prompt is the user's message, resuming the session."""
    agent = await _owned_agent_or_404(agent_id, user)
    # name an untitled chat thread after its first message, so threads are distinguishable
    if agent.get("kind") == "chat" and (agent.get("name") or "") in ("", "New chat"):
        title = " ".join(body.prompt.split())[:60]
        if title:
            await db.set_agent_name(agent_id, title)
    resume = bool(agent.get("last_session_id"))
    run_id = await db.create_run(agent_id, prompt=body.prompt, resume=resume)
    await runner.start_run(run_id, agent)
    return {"run_id": run_id}


# ==================== programmatic API (keys + /v1) ====================
# Agents are callable over HTTP with a per-agent scoped key. Owners manage keys
# from the console (cookie auth, below); external callers use Bearer sk_ag_… on
# the /v1 routes. A key can only ever reach its own agent.

@app.post("/api/agents/{agent_id}/keys")
async def create_agent_key(agent_id: int, body: ApiKeyIn, user: dict = Depends(current_user)):
    """Mint an API key for an agent. The raw key is returned ONCE."""
    agent = await _owned_agent_or_404(agent_id, user)
    raw, row = await db.create_api_key(agent_id, _org_id(user),
                                       name=body.name, created_by=user["id"])
    # include the raw key (shown once) + the public agent id the caller will use
    return {"key": raw, "public_agent_id": agent["public_id"], **row}


@app.get("/api/agents/{agent_id}/keys")
async def list_agent_keys(agent_id: int, user: dict = Depends(current_user)):
    agent = await _owned_agent_or_404(agent_id, user)
    return {"public_agent_id": agent["public_id"], "keys": await db.list_api_keys(agent_id)}


@app.delete("/api/agents/{agent_id}/keys/{key_id}")
async def revoke_agent_key(agent_id: int, key_id: int, user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    if not await db.revoke_api_key(key_id, agent_id):
        raise HTTPException(404, "key not found")
    return {"ok": True}


async def _agent_from_api_key(request: Request, public_id: str) -> dict:
    """Resolve `Authorization: Bearer sk_ag_…` to its agent and enforce that the
    key is scoped to the agent named in the path. Raises 401/403/404."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    key = await db.resolve_api_key(auth[7:].strip())
    if not key:
        raise HTTPException(401, "invalid or revoked API key")
    # per-key rate limit: a runaway/buggy client throttles itself (429) rather than
    # spawning unbounded expensive runs or starving other callers.
    allowed, retry = ratelimit.v1_limiter.check(f"key:{key['id']}")
    if not allowed:
        raise HTTPException(429, "rate limit exceeded",
                            headers={"Retry-After": str(int(retry) + 1)})
    agent = await db.get_agent_by_public_id(public_id)
    if not agent:
        raise HTTPException(404, "agent not found")
    if key["agent_id"] != agent["id"]:
        # the key exists but isn't for this agent — never reveal cross-agent info
        raise HTTPException(403, "this key is not scoped to that agent")
    return agent


@app.post("/v1/agents/{public_id}/chat")
async def v1_chat(public_id: str, body: V1ChatIn, request: Request):
    """Send a message to the agent; returns the run id. Resumes the agent session."""
    agent = await _agent_from_api_key(request, public_id)
    resume = bool(agent.get("last_session_id"))
    run_id = await db.create_run(agent["id"], prompt=body.message, resume=resume)
    await runner.start_run(run_id, agent)
    return {"run_id": run_id, "agent_id": public_id}


@app.post("/v1/agents/{public_id}/run")
async def v1_run(public_id: str, request: Request):
    """Trigger the agent with its default instruction."""
    agent = await _agent_from_api_key(request, public_id)
    run_id = await db.create_run(agent["id"], prompt=agent["prompt"])
    await runner.start_run(run_id, agent)
    return {"run_id": run_id, "agent_id": public_id}


@app.get("/v1/runs/{run_id}")
async def v1_get_run(run_id: int, request: Request,
                     public_id: str = Query(..., description="the agent's public id")):
    """Fetch a run + its events. Scoped: the key's agent must own the run."""
    agent = await _agent_from_api_key(request, public_id)
    run = await db.get_run(run_id)
    if not run or run.get("agent_id") != agent["id"]:
        raise HTTPException(404, "run not found")
    run["events"] = await db.get_events(run_id)
    return run


@app.put("/api/agents/{agent_id}/schedule")
async def set_schedule(agent_id: int, body: AgentScheduleIn,
                       user: dict = Depends(current_user)):
    """Change an agent's routine and re-register it with the scheduler."""
    await _owned_agent_or_404(agent_id, user)
    if body.schedule_kind in ("cron", "interval") and not body.schedule_expr:
        raise HTTPException(400, "schedule_expr is required for cron/interval agents")
    # validate before persisting so a paused agent can't store a broken expr
    try:
        scheduler.validate_schedule(body.schedule_kind, body.schedule_expr)
    except Exception as e:
        raise HTTPException(400, f"invalid schedule: {e}")
    await db.set_agent_schedule(agent_id, body.schedule_kind, body.schedule_expr)
    agent = await db.get_agent(agent_id)
    scheduler.remove_agent(agent_id)
    if body.schedule_kind in ("cron", "interval") and agent["enabled"]:
        scheduler.add_agent(agent)
    return await _agent_out(agent)


@app.put("/api/agents/{agent_id}/name")
async def rename_agent(agent_id: int, body: AgentNameIn,
                       user: dict = Depends(current_user)):
    """Rename an agent / chat thread (owner-scoped)."""
    await _owned_agent_or_404(agent_id, user)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    await db.set_agent_name(agent_id, name)
    return {"id": agent_id, "name": name}


@app.put("/api/agents/{agent_id}/model")
async def set_agent_model_endpoint(agent_id: int, body: AgentModelIn,
                                   user: dict = Depends(current_user)):
    """Pin an agent/chat to a provider+model — empty/omitted uses the routed default."""
    await _owned_agent_or_404(agent_id, user)
    provider = (body.provider or "").strip() or None
    model = (body.model or "").strip() or None
    if provider and not providers.get(provider):
        raise HTTPException(400, f"unknown provider '{provider}'")
    if provider and not model:
        raise HTTPException(400, "model is required when a provider is set")
    if not provider:
        model = None
    await db.set_agent_model(agent_id, provider, model)
    return {"id": agent_id, "provider": provider, "model": model}


@app.get("/api/agents/{agent_id}/alerts")
async def get_alerts(agent_id: int, user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    return await db.get_agent_alerts(agent_id)


@app.put("/api/agents/{agent_id}/alerts")
async def set_alerts(agent_id: int, body: AgentAlertsIn,
                     user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    await db.set_agent_alerts(
        agent_id, _org_id(user), [r.model_dump() for r in body.rules])
    return await db.get_agent_alerts(agent_id)


@app.get("/api/notifications")
async def list_notifications(unread_only: bool = Query(False),
                             user: dict = Depends(current_user)):
    org_id = _org_id(user)
    return {
        "items": await db.list_notifications(org_id, unread_only=unread_only),
        "unread": await db.unread_count(org_id),
    }


@app.post("/api/notifications/read")
async def mark_read(body: NotificationsReadIn, user: dict = Depends(current_user)):
    org_id = _org_id(user)
    await db.mark_notifications_read(org_id, body.ids)
    return {"unread": await db.unread_count(org_id)}


@app.post("/api/agents/{agent_id}/enable")
async def enable_agent(agent_id: int, enabled: bool = Query(True),
                       user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    await db.set_agent_enabled(agent_id, enabled)
    agent = await db.get_agent(agent_id)
    if enabled:
        scheduler.add_agent(agent)
    else:
        scheduler.remove_agent(agent_id)
    return await _agent_out(agent)


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: int, user: dict = Depends(current_user)):
    await _owned_agent_or_404(agent_id, user)
    scheduler.remove_agent(agent_id)
    await db.delete_agent(agent_id)
    await runner.remove_agent_volume(agent_id)
    return {"deleted": agent_id}


# ---------------- templates ----------------

@app.get("/api/templates")
async def list_templates(user: dict = Depends(current_user)):
    return templates.list_templates()


@app.post("/api/templates/{key}")
async def create_from_template(key: str, user: dict = Depends(current_user)):
    """Instantiate a prebuilt template as a fully-configured, runnable agent."""
    tpl = templates.get(key)
    if not tpl:
        raise HTTPException(404, "template not found")
    org_id = _org_id(user)

    agent_id = await db.create_agent({
        "name": tpl["name"],
        "prompt": tpl["prompt"],
        "system_prompt": tpl.get("system_prompt"),
        "baseline_minutes": tpl.get("baseline_minutes"),
        "schedule_kind": tpl.get("schedule_kind", "once"),
        "schedule_expr": tpl.get("schedule_expr"),
        "provider": tpl.get("provider"),
        "model": tpl.get("model"),
    }, org_id=org_id, owner_id=user["id"])

    # attach skills — reuse an existing same-named skill so re-instantiating is idempotent
    existing = {s["name"]: s["id"] for s in await db.list_skills(org_id)}
    skill_ids = []
    for s in tpl.get("skills", []):
        sid = existing.get(s["name"])
        if sid is None:
            sid = await db.create_skill(org_id, s["name"],
                                        description=s.get("description"),
                                        instructions=s.get("instructions", ""))
        skill_ids.append(sid)
    if skill_ids:
        await db.set_agent_skills(agent_id, skill_ids)

    if tpl.get("alerts"):
        await db.set_agent_alerts(agent_id, org_id, tpl["alerts"])

    agent = await db.get_agent(agent_id)
    if agent["schedule_kind"] in ("cron", "interval"):
        scheduler.add_agent(agent)
    return await _agent_out(agent)


# ---------------- runs ----------------

# The history payload for one run can be huge (a heavy agentic turn = thousands of
# tool_use/tool_result/thinking events, tens of MB). The console only ever shows a
# short preview of the noisy activity events (200-400 chars) and full assistant_text,
# so truncate the big blob fields server-side. Keeps the conversation intact while
# cutting a multi-MB payload down to a few hundred KB (fixes the client-side freeze).
_RUN_BLOB_CAP = 2000       # per-event char cap for noisy blob fields
_RUN_ACTIVITY_KEEP = 40    # most-recent noisy activity events kept per run
_ACTIVITY_TYPES = {"thinking", "tool_use", "tool_result"}

def _truncate_event(e: dict) -> dict:
    t = e.get("type")
    if t == "thinking" and isinstance(e.get("text"), str) and len(e["text"]) > _RUN_BLOB_CAP:
        return {**e, "text": e["text"][:_RUN_BLOB_CAP], "truncated": True}
    if t == "tool_result" and isinstance(e.get("content"), str) and len(e["content"]) > _RUN_BLOB_CAP:
        return {**e, "content": e["content"][:_RUN_BLOB_CAP], "truncated": True}
    if t == "tool_use" and e.get("input") is not None:
        s = json.dumps(e["input"])
        if len(s) > _RUN_BLOB_CAP:
            return {**e, "input": {"_preview": s[:_RUN_BLOB_CAP]}, "truncated": True}
    return e

def _shape_run_events(events: list[dict]) -> list[dict]:
    shaped = [_truncate_event(e) for e in events]
    # A heavy agentic turn can log thousands of tool_use/tool_result/thinking
    # events; the UI only shows short muted previews of them. Keep every
    # conversation-critical event (assistant_text, error, result, status, routed)
    # but drop the oldest noisy activity beyond the last _RUN_ACTIVITY_KEEP, with a
    # marker. Assistant messages and recent context are preserved in order.
    activity_pos = [i for i, e in enumerate(shaped) if e.get("type") in _ACTIVITY_TYPES]
    if len(activity_pos) <= _RUN_ACTIVITY_KEEP:
        return shaped
    drop = set(activity_pos[:-_RUN_ACTIVITY_KEEP])
    out, marked = [], False
    for i, e in enumerate(shaped):
        if i in drop:
            if not marked:
                out.append({"type": "omitted", "seq": e.get("seq"), "n": len(drop)})
                marked = True
            continue
        out.append(e)
    return out


@app.get("/api/runs/{run_id}")
async def get_run(run_id: int, user: dict = Depends(current_user)):
    run = await db.get_owned_run(run_id, _org_id(user))
    if not run:
        raise HTTPException(404, "run not found")
    run["events"] = _shape_run_events(await db.get_events(run_id))
    return run


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: int, user: dict = Depends(current_user)):
    run = await db.get_owned_run(run_id, _org_id(user))
    if not run:
        raise HTTPException(404, "run not found")

    async def gen():
        # replay stored events first so a late subscriber sees the whole run
        for ev in await db.get_events(run_id):
            yield f"data: {json.dumps(ev)}\n\n"
        # if the run already finished, close immediately
        current = await db.get_run(run_id)
        if current and current["status"] not in ("queued", "running"):
            yield f"data: {json.dumps({'type': 'status', 'status': current['status']})}\n\n"
            return
        # otherwise subscribe to live events
        q = runner.subscribe(run_id)
        try:
            while True:
                ev = await q.get()
                if ev is None:  # sentinel: run finished
                    break
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            runner.unsubscribe(run_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/connected")
async def connected():
    # OAuth callback landing for connectors: a tiny page that closes the popup
    # and tells the console to refresh. No auth needed (it makes no API calls).
    return FileResponse(WEB_DIR / "connected.html")


@app.get("/")
async def home():
    return FileResponse(WEB_DIR / "home.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/version")
async def version():
    # cheap, unauthenticated build id; the console polls it to auto-reload open
    # tabs after a deploy. no-store so a proxy/browser never serves a stale id.
    return JSONResponse({"version": config.APP_VERSION},
                        headers={"Cache-Control": "no-store"})


# revalidate the SPA shell on every load so a reload always gets fresh HTML
_HTML_NOCACHE = {"Cache-Control": "no-cache"}


@app.get("/app")
async def console():
    return FileResponse(WEB_DIR / "console.html", headers=_HTML_NOCACHE)


@app.get("/app-classic")
async def console_classic():
    # previous single-page console, kept as an instant rollback
    return FileResponse(WEB_DIR / "index.html", headers=_HTML_NOCACHE)


@app.get("/v2")
async def console_v2():
    return FileResponse(WEB_DIR / "console.html")


@app.get("/app/{rest:path}")
async def console_deep(rest: str):
    # SPA deep links: /app/chat/<id>, /app/routines, /app/connectors, /app/data, ...
    return FileResponse(WEB_DIR / "console.html")


@app.get("/favicon.svg")
async def favicon_svg():
    return FileResponse(WEB_DIR / "favicon.svg")


@app.get("/favicon.ico")
async def favicon_ico():
    return FileResponse(WEB_DIR / "favicon-32.png")


@app.get("/favicon-32.png")
async def favicon_png():
    return FileResponse(WEB_DIR / "favicon-32.png")


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    return FileResponse(WEB_DIR / "apple-touch-icon.png")


@app.get("/invite/{token}")
async def invite_page(token: str):
    # standalone accept page; the token is read client-side from the path
    return FileResponse(WEB_DIR / "invite.html")


@app.get("/signup/{token}")
async def signup_page(token: str):
    # own-org signup page; the token is read client-side from the path
    return FileResponse(WEB_DIR / "signup.html")


def main():
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()

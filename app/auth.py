"""Authentication: email/password + Google OAuth, backed by session cookies.

Identity flows into the app through one opaque, HttpOnly session cookie
(`sa_session`). The cookie carries a random token; only its sha256 hash is stored
(in the `sessions` table), so a DB leak doesn't hand out live sessions. Because
it's a cookie, the browser sends it automatically on the SSE `EventSource`
request too — no header juggling needed for streaming.

`current_user` is the FastAPI dependency every owned endpoint depends on; it
resolves the cookie to a user or raises 401.
"""
import hashlib
import secrets
import time

import bcrypt
from fastapi import HTTPException, Request, Response

from . import config, db

COOKIE_NAME = "sa_session"

# Google OAuth client (registered lazily; only usable when GOOGLE_ENABLED).
_oauth = None


def oauth():
    """Return a configured authlib OAuth object with Google registered."""
    global _oauth
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth
        _oauth = OAuth()
        if config.GOOGLE_ENABLED:
            _oauth.register(
                name="google",
                client_id=config.GOOGLE_CLIENT_ID,
                client_secret=config.GOOGLE_CLIENT_SECRET,
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
            )
    return _oauth


# ---------- passwords ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------- sessions ----------

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _expiry() -> str:
    ts = time.gmtime(time.time() + config.SESSION_TTL_HOURS * 3600)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", ts)


async def new_session(user_id: int) -> str:
    """Create a session row and return the raw token to put in the cookie.

    The session starts out acting in the user's home org; the org switcher (and
    `current_user`'s fallback) can change it later.
    """
    raw = secrets.token_urlsafe(32)
    user = await db.get_user(user_id)
    active_org = user.get("org_id") if user else None
    await db.create_session(_hash_token(raw), user_id, _expiry(), active_org_id=active_org)
    return raw


def set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, raw_token,
        max_age=config.SESSION_TTL_HOURS * 3600,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


async def current_user(request: Request) -> dict:
    """FastAPI dependency: resolve the session cookie to a user, else 401.

    Also resolves the caller's **active org** and **role** (teams model): from the
    session's active_org_id when the user is still a member of it, otherwise falling
    back to their home org / first membership and persisting that choice. Attaches
    `active_org_id`, `active_role`, and `orgs` (all orgs the user belongs to).
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise HTTPException(401, "not authenticated")
    token_hash = _hash_token(raw)
    user = await db.get_session_user(token_hash)
    if not user:
        raise HTTPException(401, "session expired or invalid")

    orgs = await db.list_user_orgs(user["id"])
    if not orgs and user.get("org_id"):
        # safety net for any pre-teams user without a membership row
        await db.add_membership(user["id"], user["org_id"], user.get("role") or "admin")
        orgs = await db.list_user_orgs(user["id"])
    roles = {o["id"]: o["role"] for o in orgs}

    active = user.get("active_org_id")
    if active not in roles:  # NULL (old session) or an org the user just left
        active = (user["org_id"] if user.get("org_id") in roles
                  else (orgs[0]["id"] if orgs else user.get("org_id")))
        if active is not None:
            await db.set_session_active_org(token_hash, active)

    user["active_org_id"] = active
    user["active_role"] = roles.get(active)
    user["orgs"] = orgs
    return user


def session_token_hash(request: Request) -> "str | None":
    """The hash of the caller's session token, for updating their own session row
    (e.g. switching active org). None if no cookie present."""
    raw = request.cookies.get(COOKIE_NAME)
    return _hash_token(raw) if raw else None


async def logout(request: Request) -> None:
    raw = request.cookies.get(COOKIE_NAME)
    if raw:
        await db.delete_session(_hash_token(raw))

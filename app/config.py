"""Runtime configuration, all overridable via environment variables."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# storage
DB_PATH = os.environ.get("SUPERAGENT_DB", str(BASE_DIR / "data" / "superagent.db"))

# task execution
TASK_IMAGE = os.environ.get("SUPERAGENT_TASK_IMAGE", "superagent-task:latest")
MAX_CONCURRENT = int(os.environ.get("SUPERAGENT_MAX_CONCURRENT", "3"))
TASK_TIMEOUT = int(os.environ.get("SUPERAGENT_TASK_TIMEOUT", "3600"))  # seconds
DEFAULT_MAX_TURNS = int(os.environ.get("SUPERAGENT_MAX_TURNS", "60"))
# Max size of a single stdout event line from a task container. The container
# emits one JSON event per line; a big tool result or report can be large, so we
# raise asyncio's default 64 KiB StreamReader limit well above it. Lines beyond
# this are dropped (with a log event) rather than crashing the run.
RUN_STREAM_LIMIT = int(os.environ.get("SUPERAGENT_RUN_STREAM_LIMIT", str(32 * 1024 * 1024)))

# model routing (see app/providers.py, app/router.py)
DEFAULT_PROVIDER = os.environ.get("SUPERAGENT_DEFAULT_PROVIDER", "anthropic")
PROVIDERS_FILE = os.environ.get("SUPERAGENT_PROVIDERS_FILE", str(BASE_DIR / "providers.json"))
ROUTING_FILE = os.environ.get("SUPERAGENT_ROUTING_FILE", str(BASE_DIR / "routing.json"))

# docker sandbox limits
TASK_NETWORK = os.environ.get("SUPERAGENT_TASK_NETWORK", "bridge")
TASK_MEMORY = os.environ.get("SUPERAGENT_TASK_MEMORY", "2g")
TASK_CPUS = os.environ.get("SUPERAGENT_TASK_CPUS", "2")
TASK_PIDS = os.environ.get("SUPERAGENT_TASK_PIDS", "512")

# auth passed through to task containers
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# server
HOST = os.environ.get("SUPERAGENT_HOST", "0.0.0.0")
PORT = int(os.environ.get("SUPERAGENT_PORT", "8080"))


def _compute_version() -> str:
    """A build id that changes on every deploy so open browser tabs can detect a
    new version and reload themselves. Combines the git short SHA with the served
    console's mtime, so it bumps whether we commit or just restart after an edit."""
    sha = ""
    try:
        import subprocess
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(BASE_DIR),
            stderr=subprocess.DEVNULL, timeout=2).decode().strip()
    except Exception:
        pass
    try:
        mt = int((BASE_DIR / "web" / "console.html").stat().st_mtime)
    except Exception:
        mt = 0
    return f"{sha or 'dev'}.{mt}" if mt else (sha or "dev")


# Frozen at process start → identical for the life of one server process, and
# different after any deploy+restart. Exposed via GET /api/version.
APP_VERSION = os.environ.get("SUPERAGENT_VERSION") or _compute_version()

# --- auth / multi-tenant ---
# Secret for signing the OAuth-state session middleware. Required in practice;
# a random default keeps dev working but invalidates sessions on restart.
SESSION_SECRET = os.environ.get("SUPERAGENT_SESSION_SECRET") or os.urandom(32).hex()
SESSION_TTL_HOURS = int(os.environ.get("SUPERAGENT_SESSION_TTL_HOURS", "720"))
# Set Secure on the session cookie (enable when serving over https).
COOKIE_SECURE = os.environ.get("SUPERAGENT_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# Google OAuth (optional). If both are set, "Sign in with Google" is enabled;
# otherwise only email/password login is offered.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI", "http://localhost:8080/api/auth/google/callback"
)
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# --- connectors (SaaS integrations via a managed connector platform) ---
# Which connector driver to use (see app/connectors/). Composio now; a Nango
# driver can be swapped in later without touching runner/API/UI.
CONNECTORS_PROVIDER = os.environ.get("SUPERAGENT_CONNECTORS_PROVIDER", "composio")

# Composio (managed OAuth + per-user MCP). Set the API key to enable connectors.
COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY", "")
# Per-app auth-config ids created once in the Composio dashboard (ac_...).
COMPOSIO_AUTHCONFIG = {
    "slack": os.environ.get("COMPOSIO_AUTHCONFIG_SLACK", ""),
    "linear": os.environ.get("COMPOSIO_AUTHCONFIG_LINEAR", ""),
    "gmail": os.environ.get("COMPOSIO_AUTHCONFIG_GMAIL", ""),
}
# Bring-your-own-app OAuth credentials for toolkits Composio does NOT manage
# (Shopify, Meta Ads, ...). Register a Shopify/Meta app, then set its client id +
# secret here; the driver creates a custom Composio auth config from them once.
COMPOSIO_APP_CREDENTIALS = {
    "shopify": {
        "client_id": os.environ.get("COMPOSIO_SHOPIFY_CLIENT_ID", ""),
        "client_secret": os.environ.get("COMPOSIO_SHOPIFY_CLIENT_SECRET", ""),
    },
    "metaads": {
        "client_id": os.environ.get("COMPOSIO_METAADS_CLIENT_ID", ""),
        "client_secret": os.environ.get("COMPOSIO_METAADS_CLIENT_SECRET", ""),
    },
}
# Where Composio sends the user after they finish OAuth. A small page that
# closes the popup and tells the console to refresh (see web/connected.html).
COMPOSIO_REDIRECT_URI = os.environ.get(
    "COMPOSIO_REDIRECT_URI", "http://localhost:8080/connected"
)

CONNECTORS_ENABLED = bool(COMPOSIO_API_KEY) if CONNECTORS_PROVIDER == "composio" else False

# Symmetric secret used to encrypt DB-connection credentials at rest. A dedicated
# key is best; falls back to the session secret so a stable SUPERAGENT_SESSION_SECRET
# also keeps stored DSNs decryptable across restarts.
SECRET_KEY = os.environ.get("SUPERAGENT_SECRET_KEY") or SESSION_SECRET

# Assumed fully-loaded hourly cost of the human whose work an agent replaces —
# used to convert time saved into dollars in the impact view.
HOURLY_RATE = float(os.environ.get("SUPERAGENT_HOURLY_RATE", "60"))

# Max size (MB) for a single file uploaded into a chat's workspace.
UPLOAD_MAX_MB = int(os.environ.get("SUPERAGENT_UPLOAD_MAX_MB", "25"))

# Programmatic API (/v1) per-key rate limit: sustained requests/minute (token
# bucket; burst up to V1_RATE_BURST). 0 disables the limiter.
V1_RATE_PER_MIN = int(os.environ.get("SUPERAGENT_V1_RATE_PER_MIN", "120"))
V1_RATE_BURST = int(os.environ.get("SUPERAGENT_V1_RATE_BURST", str(V1_RATE_PER_MIN)))

# Signup gating. "open" (default — right for self-hosting) allows self-signup.
# "invite" requires a pending invitation for the email — use this on a shared
# hosted instance to control who can consume its (finite) compute and your
# provider-key spend. New Google sign-ins are gated the same way.
SIGNUP_MODE = os.environ.get("SUPERAGENT_SIGNUP", "open").strip().lower()
SIGNUP_OPEN = SIGNUP_MODE != "invite"

# Instance operators: comma-separated emails allowed to onboard *new teams* via
# self-serve signup links (each new user gets their OWN fresh workspace, not the
# operator's org). Distinct from org admins, who can only invite teammates into
# their own org. If unset, any org admin may create signup links (fine for
# self-hosting); on a shared hosted instance, set this so only you can.
OPERATOR_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("SUPERAGENT_OPERATORS", "").split(",")
    if e.strip()
}

"""Composio connector driver.

Wraps the `composio` Python SDK (pinned against 0.17.1). This is the ONLY file
that imports or calls Composio — the swap boundary for a future Nango driver.

Connection model (Composio): a user is an arbitrary external id (we pass our
DB user id); each app ("toolkit": slack/linear/gmail) gets a managed-OAuth
connected account. Tools are exposed to the agent via a per-user MCP URL.

Return-object shapes vary across Composio versions, so attribute access here is
deliberately defensive (`_attr`/`_items`/`_mcp_url`). Live-verify against the
running Composio account when a key is configured.
"""
import threading
from typing import Optional

from .. import config
from .base import Connector

# Curated catalog of Composio toolkits (slugs verified against the live catalog).
# Grouped by category for the Connections UI. Connect/status/MCP logic is generic,
# so growing this list is the only change needed to offer more connectors.
_CATALOG = [
    # Communication
    ("Communication", "gmail", "Gmail"),
    ("Communication", "slack", "Slack"),
    ("Communication", "discord", "Discord"),
    ("Communication", "intercom", "Intercom"),
    # Docs & storage
    ("Docs & storage", "notion", "Notion"),
    ("Docs & storage", "googledrive", "Google Drive"),
    ("Docs & storage", "googlesheets", "Google Sheets"),
    ("Docs & storage", "airtable", "Airtable"),
    ("Docs & storage", "dropbox", "Dropbox"),
    ("Docs & storage", "box", "Box"),
    # Calendar & scheduling
    ("Calendar", "googlecalendar", "Google Calendar"),
    ("Calendar", "calendly", "Calendly"),
    # Project & dev
    ("Project & dev", "linear", "Linear"),
    ("Project & dev", "jira", "Jira"),
    ("Project & dev", "asana", "Asana"),
    ("Project & dev", "trello", "Trello"),
    ("Project & dev", "clickup", "ClickUp"),
    ("Project & dev", "github", "GitHub"),
    ("Project & dev", "figma", "Figma"),
    # CRM & sales
    ("CRM & sales", "salesforce", "Salesforce"),
    ("CRM & sales", "hubspot", "HubSpot"),
    ("CRM & sales", "gong", "Gong"),
    # Support
    ("Support", "zendesk", "Zendesk"),
    ("Support", "gorgias", "Gorgias"),
    # Incident & security
    ("Incident & security", "sentry", "Sentry"),
    ("Incident & security", "datadog", "Datadog"),
    ("Incident & security", "pagerduty", "PagerDuty"),
    ("Incident & security", "cloudflare", "Cloudflare"),
    ("Incident & security", "bitwarden", "Bitwarden"),
    # Analytics
    ("Analytics", "posthog", "PostHog"),
    ("Analytics", "mixpanel", "Mixpanel"),
    ("Analytics", "ahrefs", "Ahrefs"),
    # Marketing
    ("Marketing", "mailchimp", "Mailchimp"),
    ("Marketing", "klaviyo", "Klaviyo"),
    ("Marketing", "typefully", "Typefully"),
    ("Marketing", "googleads", "Google Ads"),
    ("Marketing", "metaads", "Meta Ads"),
    # Commerce & finance
    ("Commerce & finance", "shopify", "Shopify"),
    ("Commerce & finance", "stripe", "Stripe"),
    ("Commerce & finance", "xero", "Xero"),
    ("Commerce & finance", "brex", "Brex"),
    # HR & agreements
    ("HR & agreements", "bamboohr", "BambooHR"),
    ("HR & agreements", "ashby", "Ashby"),
    ("HR & agreements", "docusign", "DocuSign"),
    # Content & social
    ("Content & social", "youtube", "YouTube"),
    ("Content & social", "twitter", "X (Twitter)"),
    ("Content & social", "linkedin", "LinkedIn"),
    ("Content & social", "exa", "Exa"),
]
_PROVIDERS = [{"slug": s, "label": l, "category": c} for (c, s, l) in _CATALOG]


def _attr(obj, *names):
    """Read the first present, non-null attribute/key from an object or dict."""
    for n in names:
        if isinstance(obj, dict):
            if obj.get(n) is not None:
                return obj[n]
        else:
            v = getattr(obj, n, None)
            if v is not None:
                return v
    return None


def _items(resp) -> list:
    if isinstance(resp, list):
        return resp
    for n in ("items", "data", "connected_accounts", "results", "servers"):
        v = _attr(resp, n)
        if isinstance(v, list):
            return v
    return []


def _mcp_url(inst) -> Optional[str]:
    v = _attr(inst, "url", "mcp_url")
    if isinstance(v, str):
        return v
    u = _attr(inst, "user_ids_url", "user_urls", "urls")
    if isinstance(u, dict):
        for val in u.values():
            if isinstance(val, str):
                return val
            got = _attr(val, "url")
            if isinstance(got, str):
                return got
    if isinstance(u, list) and u:
        first = u[0]
        return first if isinstance(first, str) else _attr(first, "url")
    return None


class ComposioConnector(Connector):
    name = "composio"

    def __init__(self):
        from composio import Composio
        self._c = Composio(api_key=config.COMPOSIO_API_KEY)
        self._servers: dict[str, str] = {}   # toolkit -> mcp server id
        self._authconfigs: dict[str, str] = {}
        self._lock = threading.Lock()

    def providers(self) -> list[dict]:
        return [dict(p) for p in _PROVIDERS]

    def _ensure_auth_config(self, provider: str) -> Optional[str]:
        """Return an auth-config id for a toolkit: explicit > existing > create managed.

        (Composio-managed OAuth now requires an auth config + connected_accounts.link;
        the old authorize/initiate path for managed auth was retired 2026-07.)
        """
        explicit = config.COMPOSIO_AUTHCONFIG.get(provider)
        if explicit:
            return explicit
        if provider in self._authconfigs:
            return self._authconfigs[provider]
        with self._lock:
            if provider in self._authconfigs:
                return self._authconfigs[provider]
            acid = None
            try:  # reuse an existing auth config for this toolkit
                for a in _items(self._c.auth_configs.list()):
                    tk = _attr(a, "toolkit")
                    slug = _attr(tk, "slug") if tk is not None else None
                    if slug == provider:
                        acid = _attr(a, "id", "nanoid")
                        break
            except Exception:
                pass
            if not acid:
                acid = self._create_auth_config(provider)
            if acid:
                self._authconfigs[provider] = acid
            return acid

    def _create_auth_config(self, provider: str) -> Optional[str]:
        """Create an auth config: a custom one from configured app credentials for
        toolkits Composio doesn't manage (Shopify, Meta Ads), else a managed one."""
        creds = config.COMPOSIO_APP_CREDENTIALS.get(provider) or {}
        if creds.get("client_id") and creds.get("client_secret"):
            created = self._c.auth_configs.create(provider, {
                "type": "use_custom_auth",
                "auth_scheme": "OAUTH2",
                "credentials": {
                    "client_id": creds["client_id"],
                    "client_secret": creds["client_secret"],
                },
            })
            return _attr(created, "id", "nanoid")
        try:
            created = self._c.auth_configs.create(provider, {"type": "use_composio_managed_auth"})
            return _attr(created, "id", "nanoid")
        except Exception as e:  # Composio has no managed app for this toolkit
            raise RuntimeError(
                f"'{provider}' has no Composio-managed login. Register your own "
                f"{provider} app, then set COMPOSIO_{provider.upper()}_CLIENT_ID and "
                f"COMPOSIO_{provider.upper()}_CLIENT_SECRET (or create the auth config "
                f"in the Composio dashboard)."
            ) from e

    def initiate(self, user_id: int, provider: str) -> dict:
        acid = self._ensure_auth_config(provider)
        if not acid:
            raise RuntimeError(f"could not resolve a Composio auth config for {provider}")
        req = self._c.connected_accounts.link(
            str(user_id), acid,
            callback_url=config.COMPOSIO_REDIRECT_URI, allow_multiple=True,
        )
        redirect = _attr(req, "redirect_url", "redirectUrl", "redirect_uri")
        ref = _attr(req, "id", "connected_account_id", "connectedAccountId", "nanoid")
        return {"redirect_url": redirect, "ref": ref}

    def status(self, user_id: int, provider: str) -> str:
        uid = str(user_id)
        try:
            resp = self._c.connected_accounts.list(user_ids=[uid], toolkit_slugs=[provider])
        except Exception:
            return "none"
        statuses = [str(_attr(i, "status") or "").upper() for i in _items(resp)]
        if "ACTIVE" in statuses:
            return "active"
        if any(s in ("INITIATED", "INITIALIZING") for s in statuses):
            return "pending"
        return "none"

    def disconnect(self, user_id: int, provider: str) -> None:
        uid = str(user_id)
        try:
            resp = self._c.connected_accounts.list(user_ids=[uid], toolkit_slugs=[provider])
        except Exception:
            return
        for item in _items(resp):
            nid = _attr(item, "id", "nanoid")
            if nid:
                try:
                    self._c.connected_accounts.delete(nid)
                except Exception:
                    pass

    def _ensure_server(self, toolkit: str) -> Optional[str]:
        """Create-or-look-up a per-toolkit MCP server config; cache its id.

        One server per toolkit (not one giant server) so a user's agent is given
        tools for ONLY the apps they connected, not all 40+ in the catalog.
        """
        if toolkit in self._servers:
            return self._servers[toolkit]
        with self._lock:
            if toolkit in self._servers:
                return self._servers[toolkit]
            name = f"superagent-{toolkit}"
            sid = None
            try:
                for s in _items(self._c.mcp.list()):
                    if _attr(s, "name") == name:
                        sid = _attr(s, "id", "server_id")
                        break
            except Exception:
                pass
            if not sid:
                created = self._c.mcp.create(name, [toolkit], manually_manage_connections=False)
                sid = _attr(created, "id", "server_id")
            if sid:
                self._servers[toolkit] = sid
            return sid

    def agent_provisioning(self, user_id: int, providers: list[str]) -> Optional[dict]:
        """Provision the user's connected apps as ONE Composio Tool Router MCP
        server. The router exposes ~6 meta-tools (search / get-schema / execute)
        instead of every toolkit's full tool list — so a user with, e.g., github +
        clickup gets ~7K tokens of tool surface instead of ~390K (github alone is
        874 tools / ~324K tokens), which otherwise overflows the model's context
        ("Prompt is too long"). The agent searches for and executes the specific
        tools it needs at runtime. See docs: Composio Tool Router / sessions."""
        if not providers:
            return None
        try:
            # `tool_router.create` is the (soon-renamed) sessions API; mcp=True
            # returns an MCP server URL keyed to this user + these toolkits.
            session = self._c.tool_router.create(
                user_id=str(user_id), toolkits=list(providers), mcp=True,
            )
            url = str(getattr(session.mcp, "url", "") or "").strip()
        except Exception:
            return None
        if not url:
            return None
        return {
            "mcp_servers": {
                "composio": {
                    "type": "http",
                    "url": url,
                    "headers": {"x-api-key": config.COMPOSIO_API_KEY},
                }
            }
        }

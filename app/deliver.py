"""Delivery — step 3 of docs/features/outcome-jobs.md.

Produce → verify → deliver. For jobs with message destinations, the agent writes
the message to `outbox/outcome-<id>.md` and is told not to send anything itself.
Only after the outcome is verified does the engine send it — exactly once per
destination (the deliveries table's UNIQUE(outcome_id, idx) guarantees that
across retries, re-verifies and restarts).

Destinations (spec.deliver_to items):
  {"kind": "notify"}                                  in-app notification
  {"kind": "slack", "channel": "#pricing"}            via the owner's Slack connector
  {"kind": "email", "to": "a@b.co", "via": "gmail"}   via the owner's Gmail/Outlook
  {"kind": "webhook", "url": "https://…", "format": "json"|"slack"}
  {"kind": "owner"}                                   email to the routine's owner via the
                                                      platform's SMTP (SMTP_* settings)
  {"kind": "data", "label": "…"}                      informational: the agent writes it
                                                      itself and checks verify it
"""
import asyncio
import datetime
import ipaddress
import json
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse

from . import db

log = logging.getLogger("antonagents.deliver")

MESSAGE_KINDS = {"notify", "slack", "email", "webhook", "owner"}
MAX_ATTEMPTS = 3
_RETRY_S = [60, 300]                      # after 1st and 2nd failure
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MSG_CAP = 30000


class DeliveryError(Exception):
    """`permanent`: retrying can't help until someone changes the setup."""
    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_in(seconds: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------- validation ----------------

def check_webhook_url(url: str) -> Optional[str]:
    """Return an error if the URL may not be called from the server (SSRF guard):
    only http(s), and every address the host resolves to must be public."""
    try:
        u = urlparse(url or "")
    except ValueError:
        return "not a valid URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "must be an http(s) URL"
    host = u.hostname
    if host in ("localhost",) or host.endswith(".local") or host.endswith(".internal"):
        return "internal addresses are not allowed"
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        return "the host does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return "private, loopback and cloud-metadata addresses are not allowed"
    return None


def normalize_deliver_to(items: Optional[list]) -> list:
    """Validate destinations from the API. Raises ValueError with a clear message."""
    out = []
    for i, d in enumerate(items or []):
        if not isinstance(d, dict):
            raise ValueError(f"destination {i + 1} must be an object")
        kind = d.get("kind")
        if not kind:   # legacy/informational, e.g. {"postgres": "au-v6 · price_facts"}
            label = ", ".join(f"{k}: {v}" for k, v in d.items())
            out.append({"kind": "data", "label": label[:200]})
            continue
        if kind == "data":
            out.append({"kind": "data", "label": str(d.get("label") or "")[:200]})
        elif kind == "notify":
            out.append({"kind": "notify"})
        elif kind == "owner":
            out.append({"kind": "owner"})
        elif kind == "slack":
            ch = str(d.get("channel") or "").strip()
            if not ch:
                raise ValueError("a Slack destination needs a channel")
            out.append({"kind": "slack", "channel": ch[:100]})
        elif kind == "email":
            to = str(d.get("to") or "").strip()
            if not _EMAIL.match(to):
                raise ValueError("an email destination needs a valid 'to' address")
            via = d.get("via")
            if via not in (None, "gmail", "outlook"):
                raise ValueError("email 'via' must be gmail or outlook")
            out.append({"kind": "email", "to": to, **({"via": via} if via else {})})
        elif kind == "webhook":
            url = str(d.get("url") or "").strip()
            err = check_webhook_url(url)
            if err:
                raise ValueError(f"webhook URL rejected: {err}")
            fmt = d.get("format") or "json"
            if fmt not in ("json", "slack"):
                raise ValueError("webhook format must be json or slack")
            out.append({"kind": "webhook", "url": url, "format": fmt})
        else:
            raise ValueError(f"unknown destination kind '{kind}'")
    return out


def message_destinations(spec: dict) -> list:
    return [d for d in (spec.get("deliver_to") or [])
            if isinstance(d, dict) and d.get("kind") in MESSAGE_KINDS]


def describe(d: dict) -> str:
    k = d.get("kind")
    return {"notify": "in-app notification",
            "slack": f"Slack {d.get('channel')}",
            "owner": "email to the owner",
            "email": f"email to {d.get('to')}",
            "webhook": f"webhook {urlparse(d.get('url', '')).hostname or ''}",
            "data": d.get("label") or "data"}.get(k, k or "?")


def outbox_path(outcome_id: int) -> str:
    return f"outbox/outcome-{outcome_id}.md"


# ---------------- message ----------------

async def _read_outbox(agent_id: int, outcome_id: int, volume: Optional[str] = None) -> Optional[str]:
    from . import runner   # lazy: runner imports the outcome engine
    raw = await runner.read_artifact(agent_id, f"/ws/{outbox_path(outcome_id)}", volume=volume)
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    return text[:_MSG_CAP] or None


def compose(o: dict, agent: dict, message: Optional[str]) -> dict:
    """The deliverable: the agent's message (or a summary) + a verified footer."""
    r = o.get("receipt") or {}
    name = agent.get("name") or "Routine"
    if not message:
        produced = (o.get("manifest") or {}).get("produced") or []
        message = "\n".join(f"- {p}" for p in produced) if produced else (r.get("summary") or "")
    checks = r.get("checks") or []
    passed = sum(1 for c in checks if c.get("passed"))
    footer = (f"✓ {r.get('headline') or 'Delivered'} · {passed}/{len(checks)} checks passed · "
              f"attempt {o.get('attempts') or 1} · verified {(o.get('verified_at') or '')[:16].replace('T', ' ')} UTC"
              if checks else "")   # a preview (dry run / awaiting approval) has no results yet
    title = f"{name}: {r.get('headline') or 'Delivered'}"
    text = f"*{name}*\n\n{message}" + (f"\n\n_{footer}_" if footer else "")
    payload = {"agent": name, "outcome_id": o["id"], "status": o.get("status"),
               "headline": r.get("headline"), "summary": r.get("summary"), "message": message,
               "checks": checks, "attempts": o.get("attempts"), "verified_at": o.get("verified_at")}
    return {"title": title, "text": text, "plain": message + (f"\n\n{footer}" if footer else ""), "payload": payload}


# ---------------- senders ----------------

def _composio():
    from .connectors import get_connector
    c = get_connector()
    client = getattr(c, "_c", None)
    if client is None:
        raise DeliveryError("connectors aren't configured on this server")
    return client


def smtp_available() -> bool:
    from . import config
    return bool(config.SMTP_HOST and config.SMTP_FROM)


def _smtp_send(to: str, subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage
    from . import config
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = config.SMTP_FROM, to, subject[:200]
    m.set_content(body)
    if config.SMTP_TLS == "ssl":
        s = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
    else:
        s = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        if config.SMTP_TLS == "starttls":
            s.starttls()
    try:
        if config.SMTP_USER:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(m)
    finally:
        s.quit()


async def send_owner_email(owner_id: Optional[int], subject: str, body: str) -> str:
    if not smtp_available():
        raise DeliveryError("platform email isn't configured (set SMTP_HOST / SMTP_FROM)", permanent=True)
    user = await db.get_user(owner_id) if owner_id else None
    if not user or not user.get("email"):
        raise DeliveryError("the routine's owner has no email address", permanent=True)
    await asyncio.to_thread(_smtp_send, user["email"], subject, body)
    return f"emailed {user['email']}"


async def sdk_execute(slug: str, args: dict, user_id) -> tuple[bool, object, object]:
    """Run one connector tool server-side as `user_id`. Returns (ok, data, error).
    Uses the toolkit's latest version — the same one the agent's tool-router session
    used to build the arguments (the SDK refuses manual runs without a version)."""
    client = _composio()
    resp = await asyncio.to_thread(client.tools.execute, slug, args, user_id=str(user_id),
                                   dangerously_skip_version_check=True)
    get = (lambda k: resp.get(k)) if isinstance(resp, dict) else (lambda k: getattr(resp, k, None))
    return bool(get("successful")), get("data"), get("error")


async def _execute(owner_id: int, provider: str, slug: str, args: dict) -> str:
    conn = await db.get_connection(owner_id, provider)
    if not conn or conn.get("status") != "active":
        raise DeliveryError(f"{provider.capitalize()} isn't connected for the routine's owner — "
                            "connect it in Connectors")
    ok, _, err = await sdk_execute(slug, args, owner_id)
    if not ok:
        raise DeliveryError(f"{provider} rejected it: {str(err)[:200]}")
    return f"{slug} ok"


async def _send(d: dict, o: dict, agent: dict, msg: dict) -> str:
    kind = d["kind"]
    owner = agent.get("owner_id")
    if kind == "notify":
        await db.add_notification(o["org_id"], o["agent_id"], None, msg["title"], msg["plain"][:2000])
        return "notification created"
    if kind == "owner":
        return await send_owner_email(owner, msg["title"], msg["plain"])
    if kind == "slack":
        return await _execute(owner, "slack", "SLACK_SEND_MESSAGE",
                              {"channel": d["channel"], "markdown_text": msg["text"]})
    if kind == "email":
        via = d.get("via")
        if not via:   # whichever mailbox the owner has connected
            for p in ("gmail", "outlook"):
                c = await db.get_connection(owner, p)
                if c and c.get("status") == "active":
                    via = p
                    break
            via = via or "gmail"
        if via == "gmail":
            args = {"recipient_email": d["to"], "subject": msg["title"], "body": msg["plain"],
                    "is_html": False}
            return await _execute(owner, "gmail", "GMAIL_SEND_EMAIL", args)
        args = {"to": d["to"], "subject": msg["title"], "body": msg["plain"], "is_html": False}
        return await _execute(owner, "outlook", "OUTLOOK_SEND_EMAIL", args)
    if kind == "webhook":
        err = await asyncio.to_thread(check_webhook_url, d["url"])   # re-check: DNS may change
        if err:
            raise DeliveryError(f"webhook URL rejected: {err}")
        import httpx
        body = {"text": msg["text"]} if d.get("format") == "slack" else msg["payload"]
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as hc:
            r = await hc.post(d["url"], json=body)
        if r.status_code >= 300:
            raise DeliveryError(f"webhook answered HTTP {r.status_code}")
        return f"HTTP {r.status_code}"
    raise DeliveryError(f"unknown destination kind '{kind}'")


# ---------------- orchestration ----------------

async def deliver_outcome(outcome_id: int, force: bool = False) -> list[dict]:
    """Send a verified outcome to each message destination, exactly once.
    `force` retries destinations that already used up their attempts."""
    o = await db.get_outcome(outcome_id)
    if not o or o["status"] != "delivered" or o.get("trigger") in ("backfill", "dry_run"):
        return (o or {}).get("deliveries") or []
    spec = await db.get_spec(o["spec_id"]) if o.get("spec_id") else None
    agent = await db.get_agent(o["agent_id"])
    if not spec or not agent:
        return o.get("deliveries") or []
    dests = [(i, d) for i, d in enumerate(spec.get("deliver_to") or [])
             if isinstance(d, dict) and d.get("kind") in MESSAGE_KINDS]
    if not dests:
        return []
    msg = compose(o, agent, await _read_outbox(agent["id"], outcome_id))
    for idx, d in dests:
        row = await db.claim_delivery(outcome_id, idx, d["kind"], describe(d))
        if row["status"] == "sent":
            continue                                    # exactly once
        if row["status"] == "failed" and not force:
            if row["attempts"] >= MAX_ATTEMPTS or not row.get("next_try_at") or row["next_try_at"] > _now():
                continue
        try:
            resp = await _send(d, o, agent, msg)
            await db.mark_delivery(row["id"], status="sent", response=resp[:300])
        except Exception as e:  # noqa: BLE001 - one destination failing never blocks the others
            err = str(e) if isinstance(e, DeliveryError) else f"{type(e).__name__}: {e}"
            tried = row["attempts"] + 1
            nxt = _iso_in(_RETRY_S[min(tried - 1, len(_RETRY_S) - 1)]) if tried < MAX_ATTEMPTS else None
            await db.mark_delivery(row["id"], status="failed", error=err[:300], next_try_at=nxt)
            if tried >= MAX_ATTEMPTS:
                from . import escalate   # lazy: escalate sends through this module
                escalate.fire(outcome_id, "delivery_failed", f"{describe(d)} — {err[:300]}")
                try:
                    await db.add_notification(o["org_id"], o["agent_id"], None,
                                              f"Couldn't deliver: {agent.get('name')}",
                                              f"{describe(d)} — {err[:300]}")
                except Exception:  # noqa: BLE001
                    log.exception("could not add delivery notification")
    deliveries = await db.list_deliveries(outcome_id)
    receipt = dict(o.get("receipt") or {})
    receipt["deliveries"] = [{"kind": x["kind"], "target": x["target"], "status": x["status"],
                              "error": x["last_error"], "sent_at": x["sent_at"]} for x in deliveries]
    await db.update_outcome(outcome_id, receipt_json=json.dumps(receipt))
    return deliveries


async def retry_due() -> None:
    for oid in await db.due_delivery_retries():
        try:
            await deliver_outcome(oid)
        except Exception:  # noqa: BLE001
            log.exception("delivery retry failed for outcome %s", oid)

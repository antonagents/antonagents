"""Escalations — tell a person, outside the app, when an outcome needs them.

The in-app notification and Needs-you inbox always happen; this adds the
channels a person actually watches. Destinations come from the job spec's
`escalate_to` (same kinds as delivery: owner email, Slack, email, webhook).
A spec without the key escalates to the owner's email when platform SMTP is set
up; an explicit empty list means in-app only. Each escalation is sent once per
destination per round (escalations table), retried up to 3 times.
"""
import asyncio
import datetime
import logging
from typing import Optional

from . import config, db, deliver

log = logging.getLogger("antonagents.escalate")

KINDS = {"owner", "slack", "email", "webhook"}
EVENTS = {
    "needs_you": "Needs you",
    "approval": "Approve",
    "failed": "Not delivered",
    "delivery_failed": "Couldn't deliver",
}
MAX_ATTEMPTS = 3
_RETRY_S = [60, 300]


def destinations(spec: Optional[dict]) -> list[dict]:
    if spec and spec.get("escalate_to") is not None:
        return [d for d in spec["escalate_to"] if isinstance(d, dict) and d.get("kind") in KINDS]
    return [{"kind": "owner"}] if deliver.smtp_available() else []


def normalize(items: Optional[list]) -> Optional[list]:
    """Validate an escalate_to list from the API (None = use the default)."""
    if items is None:
        return None
    out = deliver.normalize_deliver_to(items)
    bad = [d["kind"] for d in out if d["kind"] not in KINDS]
    if bad:
        raise ValueError(f"escalations can go to the owner, Slack, email or a webhook (not {bad[0]})")
    return out


def link(agent_id: int) -> str:
    return f"{config.PUBLIC_URL}/app/routines/{agent_id}" if config.PUBLIC_URL else ""


def _message(event: str, agent: dict, o: dict, reason: str) -> dict:
    name = agent.get("name") or "Routine"
    title = f"{EVENTS[event]}: {name}"
    what = {"needs_you": "The job stopped and can't finish without you.",
            "approval": "The automatic checks passed. Nothing is sent until you approve it.",
            "failed": "The job gave up without a verified outcome.",
            "delivery_failed": "The outcome was verified but couldn't be delivered."}[event]
    url = link(agent["id"])
    plain = f"{what}\n\n{reason}\n\n" + (f"Open it: {url}\n" if url else "") + \
        f"(outcome #{o['id']}, attempt {o.get('attempts') or 1})"
    text = f"*{title}*\n{what}\n{reason}" + (f"\n<{url}|Open in AntonAgents>" if url else "")
    payload = {"event": event, "agent": name, "agent_id": agent["id"], "outcome_id": o["id"],
               "reason": reason, "url": url or None, "attempts": o.get("attempts")}
    return {"title": title, "text": text, "plain": plain, "payload": payload}


def _iso_in(seconds: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _try(row: dict, d: dict, o: dict, agent: dict, msg: dict) -> None:
    try:
        await deliver._send(d, o, agent, msg)
        await db.mark_escalation(row["id"], status="sent")
    except Exception as e:  # noqa: BLE001 - one channel failing never blocks the others
        err = str(e) if isinstance(e, deliver.DeliveryError) else f"{type(e).__name__}: {e}"
        tried = row["attempts"] + 1
        retry = tried < MAX_ATTEMPTS and not getattr(e, "permanent", False)
        nxt = _iso_in(_RETRY_S[min(tried - 1, len(_RETRY_S) - 1)]) if retry else None
        await db.mark_escalation(row["id"], status="failed", error=err[:300], next_try_at=nxt)
        log.warning("escalation %s (%s) failed: %s", row["id"], row["kind"], err[:200])


async def escalate(outcome_id: int, event: str, reason: str) -> list[dict]:
    """Send one escalation to every configured destination, once per round."""
    o = await db.get_outcome(outcome_id)
    if not o or o.get("trigger") in ("backfill", "dry_run"):
        return []
    spec = await db.get_spec(o["spec_id"]) if o.get("spec_id") else None
    agent = await db.get_agent(o["agent_id"])
    dests = destinations(spec)
    if not agent or not dests:
        return []
    msg = _message(event, agent, o, reason or "")
    rnd = len(o.get("recovery") or [])
    sent = []
    for idx, d in enumerate(dests):
        row = await db.claim_escalation(outcome_id, event, rnd, idx, d["kind"], deliver.describe(d),
                                        msg["title"], msg["plain"])
        if row is None:
            continue                                   # already escalated this round
        await _try(row, d, o, agent, msg)
        sent.append(row)
    return sent


def fire(outcome_id: int, event: str, reason: str) -> None:
    """Fire-and-forget from the engine; never raises."""
    async def _run():
        try:
            await escalate(outcome_id, event, reason)
        except Exception:  # noqa: BLE001
            log.exception("escalation failed for outcome %s", outcome_id)
    asyncio.create_task(_run())


async def retry_due() -> None:
    for row in await db.due_escalations():
        try:
            o = await db.get_outcome(row["outcome_id"])
            spec = await db.get_spec(o["spec_id"]) if o and o.get("spec_id") else None
            agent = await db.get_agent(o["agent_id"]) if o else None
            dests = destinations(spec)
            if not o or not agent or row["idx"] >= len(dests):
                await db.mark_escalation(row["id"], status="failed", error=row["last_error"])
                continue
            msg = _message(row["event"], agent, o, "")
            msg.update(title=row["title"] or msg["title"], plain=row["body"] or msg["plain"])
            msg["text"] = f"*{msg['title']}*\n{msg['plain']}"
            await _try(row, dests[row["idx"]], o, agent, msg)
        except Exception:  # noqa: BLE001
            log.exception("escalation retry failed for %s", row["id"])

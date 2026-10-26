"""Outcome ledger — step 5 of docs/features/outcome-jobs.md.

The home number is no longer "runs completed" but outcomes: how many jobs were
verified-delivered, how many of those needed nobody, what each verified outcome
cost, and what keeps getting in the way. Dry runs never count; backfills count as
outcomes but not as "without a human" evidence of the recovery loop.
"""
import datetime
import json
import statistics
from typing import Optional

from . import config, db, metering
from .outcomes import LABELS

_SETTLED = ("delivered", "failed", "needs_you")


def _ts(iso: Optional[str]) -> Optional[datetime.datetime]:
    if not iso:
        return None
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hours(a: Optional[str], b: Optional[str]) -> Optional[float]:
    x, y = _ts(a), _ts(b)
    return (y - x).total_seconds() / 3600 if x and y else None


def _human(o: dict) -> bool:
    """Did a person have to step in (answer, retry or dismiss)?"""
    return any(e.get("class") == "user" for e in o["recovery"])


async def build(org_id: int, days: int = 30) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = await db.ledger_rows(org_id, since)
    outs = raw["outcomes"]
    for o in outs:
        o["recovery"] = json.loads(o.pop("recovery_json") or "[]")
        o["runs"] = raw["runs"].get(o["id"], [])
        o["cost"] = sum(r["cost_usd"] or 0 for r in o["runs"])
        o["tokens"] = sum(metering.tokens(json.loads(r["usage_json"]) if r["usage_json"] else None)
                          for r in o["runs"])
        o["metered"] = bool(o["runs"]) and all(r["cost_basis"] == metering.METERED for r in o["runs"])
        o["has_usage"] = bool(o["runs"]) and all(r["usage_json"] for r in o["runs"])

    def roll(group: list[dict]) -> dict:
        by = {s: 0 for s in ("delivered", "failed", "needs_you", "awaiting_approval",
                             "recovering", "running", "verifying")}
        for o in group:
            by[o["status"]] = by.get(o["status"], 0) + 1
        delivered = [o for o in group if o["status"] == "delivered"]
        good = [o for o in delivered if not o["flagged_at"]]
        settled = sum(by[s] for s in _SETTLED)
        unattended = [o for o in good if not _human(o)]
        live = [o for o in group if o["trigger"] != "backfill"]
        spend = sum(o["cost"] for o in group)
        tok = [o for o in group if o["has_usage"]]
        return {
            "outcomes": len(group), "by_status": by,
            "delivered": len(delivered), "flagged": len(delivered) - len(good),
            "settled": settled,
            "unattended": len(unattended),
            "unattended_rate": round(len(unattended) / settled, 3) if settled else None,
            "first_try": sum(1 for o in good if (o["attempts"] or 1) == 1),
            "recovered": sum(1 for o in unattended if (o["attempts"] or 1) > 1),
            "false_delivered_rate": round((len(delivered) - len(good)) / len(delivered), 3) if delivered else None,
            "minutes_saved": sum(o["baseline_minutes"] or 0 for o in good),
            "baseline_missing": len({o["agent_id"] for o in good if not o["baseline_minutes"]}),
            "spend_usd": round(spend, 4),
            "cost_per_verified_usd": round(spend / len(good), 4) if good else None,
            "cost_basis": ("metered" if group and all(o["metered"] for o in group)
                           else "estimate" if group else None),
            "tokens_per_verified": (round(sum(o["tokens"] for o in tok) / len(good))
                                    if good and tok and len(tok) == len(group) else None),
            "live_outcomes": len(live),
            # step 6: how each verified outcome was proven, and the spot-check sample
            "by_strength": {k: sum(1 for o in good if (o["strength"] or "verified") == k)
                            for k in ("verified", "judged", "accepted")},
            "spot_checks": {"requested": sum(1 for o in group if o["review_requested_at"]),
                            "reviewed": sum(1 for o in group if o["review_requested_at"] and o["reviewed_at"]),
                            "rejected": sum(1 for o in group if o["review_requested_at"]
                                            and o["review_verdict"] == "wrong")},
        }

    total = roll(outs)
    total["value_usd"] = round(total["minutes_saved"] / 60 * config.HOURLY_RATE, 2)

    # escalations: every time the engine asked for a person, and how long it waited
    waits, escalations = [], 0
    for o in outs:
        for i, e in enumerate(o["recovery"]):
            if e.get("action") != "needs_you":
                continue
            escalations += 1
            answer = next((x for x in o["recovery"][i + 1:] if x.get("class") == "user"), None)
            h = _hours(e.get("at"), answer.get("at")) if answer else None
            if h is not None:
                waits.append(h)
    open_ = [o for o in outs if o["status"] == "needs_you"]
    open_ages = [_hours(next((e.get("at") for e in reversed(o["recovery"])
                              if e.get("action") == "needs_you"), o["finished_at"]),
                        now.strftime("%Y-%m-%dT%H:%M:%SZ")) for o in open_]
    open_ages = [a for a in open_ages if a is not None]

    # what keeps getting in the way (every failed attempt, by class)
    blockers: dict[str, dict] = {}
    for o in outs:
        for e in o["recovery"]:
            cls = e.get("class")
            if not cls or cls == "user":
                continue
            b = blockers.setdefault(cls, {"class": cls, "label": LABELS.get(cls, cls), "count": 0,
                                          "routines": set(), "example": None})
            b["count"] += 1
            b["routines"].add(o["name"])
            b["example"] = (e.get("detail") or "")[:200] or b["example"]
    top = sorted(blockers.values(), key=lambda b: -b["count"])[:5]
    for b in top:
        b["routines"] = sorted(b["routines"])

    # per day, for the strip
    days_ = {}
    for i in range(days):
        d = (now - datetime.timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        days_[d] = {"date": d, "delivered": 0, "failed": 0, "needs_you": 0, "other": 0}
    for o in outs:
        d = (o["started_at"] or "")[:10]
        if d in days_:
            k = o["status"] if o["status"] in ("delivered", "failed", "needs_you") else "other"
            days_[d][k] += 1

    per = {}
    for o in outs:
        per.setdefault(o["agent_id"], []).append(o)
    routines = []
    for aid, group in per.items():
        r = roll(group)
        last = group[-1]
        routines.append({"agent_id": aid, "name": group[0]["name"],
                         "baseline_minutes": group[0]["baseline_minutes"],
                         "last": {"id": last["id"], "status": last["status"], "at": last["started_at"],
                                  "flagged": bool(last["flagged_at"])}, **r})
    routines.sort(key=lambda r: -r["outcomes"])

    return {
        "days": days, "since": since, "hourly_rate": config.HOURLY_RATE,
        "totals": total,
        "escalations": {"count": escalations, "open": len(open_),
                        "median_wait_h": round(statistics.median(waits), 2) if waits else None,
                        "oldest_open_h": round(max(open_ages), 1) if open_ages else None},
        "blockers": top, "daily": list(days_.values()), "routines": routines,
        "unverified": raw["unverified"],
    }

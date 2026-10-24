"""Outcome engine — step 1 of docs/features/outcome-jobs.md.

A routine with a published job spec turns each run into an attempt toward an
outcome. When the attempt finishes, the outcome is verified independently
(app/verify.py) and gets a receipt. "Delivered" means the required checks
passed, not that the agent said it was done.

Step 1 has no recovery loop yet: a failed check marks the outcome failed and
notifies the workspace. Agents without a published spec are untouched.
"""
import datetime
import json
import logging
import re
from typing import Optional

from . import db, verify

log = logging.getLogger("antonagents.outcomes")

_MANIFEST = re.compile(r"```manifest\s*\n(.*?)```", re.S)
_STRENGTH_RANK = {"verified": 0, "judged": 1, "accepted": 2}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prompt_addendum(spec: dict) -> str:
    """Tell the agent the definition of done and ask for a delivery manifest."""
    checks = "\n".join(f"- {c['name']}" for c in spec["checks"])
    return (
        "---\n"
        "DEFINITION OF DONE — this job is verified independently after you finish; "
        "your own report is not the proof.\n"
        f"Objective: {spec.get('objective') or '(see task above)'}\n"
        f"Acceptance checks:\n{checks}\n\n"
        "At the very end of your final message add a fenced block:\n"
        "```manifest\n"
        '{"produced": ["what you produced"], "where": ["where it landed"], '
        '"blocked": null}\n'
        "```\n"
        'If you could not complete the job, set "blocked" to the reason instead of '
        "claiming success."
    )


def parse_manifest(text: Optional[str]) -> Optional[dict]:
    m = _MANIFEST.search(text or "")
    if not m:
        return None
    try:
        v = json.loads(m.group(1))
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


async def begin_attempt(run_id: int, agent: dict, run: dict) -> Optional[str]:
    """Called when a run starts. If the agent has a published spec, attach the run
    to an outcome and return the prompt addendum; otherwise return None."""
    if (agent.get("kind") or "routine") == "chat":
        return None
    spec = await db.get_published_spec(agent["id"])
    if not spec or not spec["checks"]:
        return None
    if not run.get("outcome_id"):
        oid = await db.create_outcome(agent["id"], agent["org_id"], spec, trigger="run",
                                      started_at=run.get("created_at"))
        await db.link_run_to_outcome(run_id, oid)
    return prompt_addendum(spec)


async def finish_attempt(run_id: int) -> None:
    """Called after a run finishes. Never raises."""
    try:
        run = await db.get_run(run_id)
        if run and run.get("outcome_id"):
            await verify_outcome(run["outcome_id"], attempt=run.get("attempt") or 1)
    except Exception:  # noqa: BLE001
        log.exception("outcome verification failed for run %s", run_id)


def _secs(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return int((datetime.datetime.fromisoformat(b.replace("Z", "+00:00"))
                    - datetime.datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds())
    except (AttributeError, ValueError):
        return None


def _receipt(o: dict, spec: dict, results: list[dict], run: Optional[dict],
             delivered: bool, strength: str) -> dict:
    required = [r for r in results if r["required"]]
    failed = [r for r in required if not r["passed"]]
    if delivered:
        headline = f"Delivered · {strength.capitalize()}"
        summary = f"All {len(required)} required check{'s' if len(required) != 1 else ''} passed."
    else:
        headline = "Not verified"
        summary = (f"{len(failed)} of {len(required)} required checks failed: "
                   + "; ".join(r["name"] for r in failed))
    note = None
    if run and run.get("status") == "failed" and delivered:
        note = f"The run reported '{(run.get('error') or 'failure')[:80]}', but the outcome verified."
    elif run and run.get("status") == "succeeded" and not delivered:
        note = "The agent reported success, but verification did not confirm the outcome."
    return {
        "headline": headline, "status": "delivered" if delivered else "failed",
        "strength": strength if delivered else None, "summary": summary, "note": note,
        "objective": spec.get("objective"),
        "checks": [{"name": r["name"], "passed": r["passed"], "observed": r.get("observed"),
                    "expected": r.get("expected"), "required": r["required"]} for r in results],
        "attempts": o.get("attempts"),
        "run_id": run and run.get("id"), "run_status": run and run.get("status"),
        "runtime_s": run and _secs(run.get("started_at"), run.get("finished_at")),
        "cost_usd": run and run.get("cost_usd"),
        "verified_at": _now(),
    }


async def verify_outcome(outcome_id: int, attempt: Optional[int] = None) -> dict:
    """Run the spec's checks for an outcome, store results + receipt, set status."""
    o = await db.get_outcome(outcome_id)
    spec = await db.get_spec(o["spec_id"]) if o and o.get("spec_id") else None
    if not o or not spec:
        raise ValueError("outcome or its spec not found")
    attempt = attempt or o["attempts"] or 1
    await db.update_outcome(outcome_id, status="verifying")

    run_row = next((r for r in o["runs"] if r["attempt"] == attempt), None)
    run = await db.get_run(run_row["id"]) if run_row else None
    start = o["started_at"] or o["created_at"]
    ctx = {
        "date": start[:10],                         # UTC date the outcome started
        "outcome_start": start,
        "outcome_end": (run and run.get("finished_at")) or None,
        "org_id": o["org_id"], "agent_id": o["agent_id"],
    }
    results = await verify.run_checks(spec["checks"], ctx)
    await db.replace_check_results(outcome_id, attempt, results)

    required = [r for r in results if r["required"]]
    delivered = bool(required) and all(r["passed"] for r in required)
    strength = max((r["strength"] for r in required),
                   key=lambda s: _STRENGTH_RANK.get(s, 9), default="verified")
    receipt = _receipt(o, spec, results, run, delivered, strength)
    manifest = parse_manifest(run and run.get("result"))
    now = _now()
    await db.update_outcome(
        outcome_id, status="delivered" if delivered else "failed",
        finished_at=now, verified_at=now if delivered else None,
        receipt_json=json.dumps(receipt), cost_usd=run and run.get("cost_usd"),
        manifest_json=json.dumps(manifest) if manifest else None)

    if not delivered:
        agent = await db.get_agent(o["agent_id"])
        try:
            await db.add_notification(
                o["org_id"], o["agent_id"], run and run.get("id"),
                f"Outcome not verified: {(agent or {}).get('name', 'routine')}",
                receipt["summary"])
        except Exception:  # noqa: BLE001
            log.exception("could not add outcome notification")
    return await db.get_outcome(outcome_id)


async def backfill_from_run(run_id: int, agent: dict) -> int:
    """Create an outcome for an already-finished run and verify it (pilot/migration)."""
    spec = await db.get_published_spec(agent["id"])
    if not spec:
        raise ValueError("the agent has no published job spec")
    run = await db.get_run(run_id)
    if not run or run["agent_id"] != agent["id"]:
        raise ValueError("run not found for this agent")
    if run.get("outcome_id"):
        return run["outcome_id"]
    oid = await db.create_outcome(agent["id"], agent["org_id"], spec, trigger="backfill",
                                  started_at=run.get("started_at") or run.get("created_at"))
    await db.link_run_to_outcome(run_id, oid)
    await verify_outcome(oid)
    return oid


async def resume_pending() -> None:
    """At boot: verify outcomes a restart left in running/verifying."""
    try:
        for o in await db.pending_outcomes():
            last = await db.get_outcome(o["id"])
            runs = (last or {}).get("runs") or []
            if runs and runs[-1]["status"] in ("queued", "running"):
                continue
            await verify_outcome(o["id"])
    except Exception:  # noqa: BLE001
        log.exception("resume_pending failed")

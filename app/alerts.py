"""Alert-rule evaluation → in-app notifications.

Rules are per-agent (see db.alert_rules). Run-outcome rules are evaluated when a
run finishes (called from the runner); the schedule-miss rule is evaluated when
APScheduler reports a missed job (called from the scheduler). Every match creates
one notification scoped to the agent's org.
"""
from typing import Optional

from . import db

# supported rule kinds (kept in sync with models.AlertRuleIn)
KINDS = ("on_failure", "on_success", "result_contains", "schedule_miss")


async def evaluate_run(run_id: int) -> None:
    """Create notifications for any run-outcome rules the finished run satisfies."""
    run = await db.get_run(run_id)
    if not run:
        return
    agent = await db.get_agent(run["agent_id"])
    if not agent:
        return
    rules = await db.get_agent_alerts(agent["id"])
    if not rules:
        return

    status = run["status"]
    result = run.get("result") or ""
    name = agent["name"]

    for r in rules:
        if not r["enabled"]:
            continue
        kind, cond = r["kind"], (r.get("condition") or "")
        title = body = None
        if kind == "on_failure" and status == "failed":
            title = f'{name}: run #{run_id} failed'
            body = (run.get("error") or "")[:500]
        elif kind == "on_success" and status == "succeeded":
            title = f'{name}: run #{run_id} succeeded'
            body = result[:500]
        elif (kind == "result_contains" and status == "succeeded"
              and cond and cond.lower() in result.lower()):
            title = f'{name}: output matched "{cond}"'
            body = result[:500]
        if title is not None:
            await db.add_notification(agent["org_id"], agent["id"], run_id, title, body or "")


async def notify_schedule_miss(agent_id: int) -> None:
    """Create a miss notification if the agent has an enabled schedule_miss rule."""
    agent = await db.get_agent(agent_id)
    if not agent:
        return
    rules = await db.get_agent_alerts(agent_id)
    if any(r["kind"] == "schedule_miss" and r["enabled"] for r in rules):
        await db.add_notification(
            agent["org_id"], agent_id, None,
            f'{agent["name"]}: scheduled run missed',
            "The scheduler missed a planned run for this agent.",
        )

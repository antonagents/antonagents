"""Recurring-agent scheduling via APScheduler.

An agent with schedule_kind 'cron' or 'interval' gets an APScheduler job that,
when it fires, creates a new run and hands it to the runner. One-shot ('once')
agents are not scheduled here — they run only on manual trigger or chat.
"""
import asyncio
from typing import Optional

from apscheduler.events import EVENT_JOB_MISSED
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import alerts, db, runner

_scheduler: Optional[AsyncIOScheduler] = None


def _trigger(agent: dict):
    kind = agent["schedule_kind"]
    expr = agent.get("schedule_expr") or ""
    if kind == "cron":
        return CronTrigger.from_crontab(expr)
    if kind == "interval":
        return IntervalTrigger(seconds=int(expr))
    raise ValueError(f"agent {agent['id']} is not schedulable ({kind})")


def validate_schedule(kind: str, expr: Optional[str]) -> None:
    """Raise ValueError if kind/expr don't form a valid trigger. Cheap, no scheduler needed."""
    if kind not in ("cron", "interval"):
        return
    _trigger({"id": 0, "schedule_kind": kind, "schedule_expr": expr})


async def _fire(agent_id: int) -> None:
    agent = await db.get_agent(agent_id)
    if not agent or not agent["enabled"]:
        return
    run_id = await db.create_run(agent_id, prompt=agent.get("prompt") or "")
    await runner.start_run(run_id, agent)


def _job_id(agent_id: int) -> str:
    return f"agent-{agent_id}"


def _on_missed(event) -> None:
    """APScheduler fires this when a job's run time is missed past its grace window."""
    jid = getattr(event, "job_id", "") or ""
    if not jid.startswith("agent-"):
        return
    try:
        agent_id = int(jid.split("-", 1)[1])
    except (ValueError, IndexError):
        return
    asyncio.create_task(alerts.notify_schedule_miss(agent_id))


async def start() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)
    _scheduler.start()
    for agent in await db.list_scheduled_agents():
        add_agent(agent)


def add_agent(agent: dict) -> None:
    if _scheduler is None or agent["schedule_kind"] not in ("cron", "interval"):
        return
    _scheduler.add_job(
        _fire, trigger=_trigger(agent), args=[agent["id"]],
        id=_job_id(agent["id"]), replace_existing=True, misfire_grace_time=300,
    )


def remove_agent(agent_id: int) -> None:
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(agent_id))
    except Exception:
        pass


def next_run_at(agent_id: int) -> Optional[str]:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_job_id(agent_id))
    if job and job.next_run_time:
        return job.next_run_time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return None


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)

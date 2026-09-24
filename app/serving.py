"""Serving agents to external callers: the shared plumbing behind /v1 and MCP.

An agent is reachable from outside the console in two shapes, both authenticated
by the same per-agent `sk_ag_…` key:

- REST (/v1): start a run, then either poll it, stream it (SSE), or block on it
  with `?wait=N` so one request returns the finished answer.
- MCP (/v1/agents/{public_id}/mcp): the agent as an MCP server whose `ask` tool
  runs it — see mcp_server.py.

Both go through the helpers here so a run started over MCP behaves exactly like
one started over REST or from the console: same Docker-per-run isolation, same
connectors/skills/data sources, same run history.
"""
import asyncio
import json
from typing import AsyncIterator, Awaitable, Callable, Optional

from . import db, runner

TERMINAL = {"succeeded", "failed", "interrupted"}


async def start_chat(agent: dict, message: str) -> int:
    """Start a chat turn: `message` is the prompt and the agent's session resumes."""
    resume = bool(agent.get("last_session_id"))
    run_id = await db.create_run(agent["id"], prompt=message, resume=resume)
    await runner.start_run(run_id, agent)
    return run_id


async def start_default(agent: dict) -> int:
    """Start a run of the agent's standing instruction (like a manual trigger)."""
    run_id = await db.create_run(agent["id"], prompt=agent["prompt"])
    await runner.start_run(run_id, agent)
    return run_id


async def wait_for_run(run_id: int, timeout: float, *,
                       on_event: Optional[Callable[[Optional[dict]], Awaitable[None]]] = None,
                       tick: float = 15.0) -> Optional[dict]:
    """Block until the run reaches a terminal status or `timeout` seconds pass,
    then return the run row (which may still be queued/running on timeout).

    `on_event` is awaited for each live event, and with None every `tick` seconds
    of silence — callers use that as a keepalive for long-held connections.
    """
    # subscribe BEFORE reading the status: the runner publishes its close
    # sentinel only after finish_run, so either the row is already terminal or
    # the sentinel is still to come — no gap where we'd miss the finish.
    q = runner.subscribe(run_id)
    try:
        run = await db.get_run(run_id)
        if not run or run["status"] in TERMINAL:
            return run
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                ev = await asyncio.wait_for(q.get(), timeout=min(remaining, tick))
            except asyncio.TimeoutError:
                if on_event and loop.time() < deadline:
                    await on_event(None)
                continue
            if ev is None:  # sentinel: run finished
                break
            if on_event:
                await on_event(ev)
    finally:
        runner.unsubscribe(run_id, q)
    return await db.get_run(run_id)


def run_summary(run: dict, public_id: str) -> dict:
    """The compact, caller-facing view of a run (no event log)."""
    return {
        "run_id": run["id"],
        "agent_id": public_id,
        "status": run["status"],
        "done": run["status"] in TERMINAL,
        "result": run.get("result"),
        "error": run.get("error"),
        "cost_usd": run.get("cost_usd"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


async def sse_events(run_id: int) -> AsyncIterator[str]:
    """SSE frames for a run: replay stored events, then follow live ones until the
    run finishes. Shared by the console stream and the /v1 stream."""
    # subscribe before replaying so an event emitted between the replay query and
    # the subscription is not lost; dedupe the overlap by seq.
    q = runner.subscribe(run_id)
    try:
        last_seq = 0
        for ev in await db.get_events(run_id):
            last_seq = max(last_seq, ev.get("seq") or 0)
            yield f"data: {json.dumps(ev)}\n\n"
        current = await db.get_run(run_id)
        if current and current["status"] not in ("queued", "running"):
            yield f"data: {json.dumps({'type': 'status', 'status': current['status']})}\n\n"
            return
        while True:
            ev = await q.get()
            if ev is None:  # sentinel: run finished
                break
            # status frames aren't stored (and reuse the last seq), so never
            # treat them as replayed duplicates
            seq = ev.get("seq")
            if ev.get("type") != "status" and seq is not None and seq <= last_seq:
                continue
            yield f"data: {json.dumps(ev)}\n\n"
    finally:
        runner.unsubscribe(run_id, q)

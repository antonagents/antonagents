"""Outcome engine — step 1 of docs/features/outcome-jobs.md.

A routine with a published job spec turns each run into an attempt toward an
outcome. When the attempt finishes, the outcome is verified independently
(app/verify.py) and gets a receipt. "Delivered" means the required checks
passed, not that the agent said it was done.

Step 1 has no recovery loop yet: a failed check marks the outcome failed and
notifies the workspace. Agents without a published spec are untouched.
"""
import asyncio
import datetime
import json
import logging
import re
from typing import Optional

from . import config, db, deliver, verify

log = logging.getLogger("antonagents.outcomes")

_MANIFEST = re.compile(r"```manifest\s*\n(.*?)```", re.S)
_STRENGTH_RANK = {"verified": 0, "judged": 1, "accepted": 2}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prompt_addendum(spec: dict, outcome_id: Optional[int] = None) -> str:
    """Tell the agent the definition of done and ask for a delivery manifest."""
    checks = "\n".join(f"- {c['name']}" for c in spec["checks"])
    dests = deliver.message_destinations(spec)
    delivery = ""
    if dests and outcome_id:
        where = ", ".join(deliver.describe(d) for d in dests)
        delivery = (
            f"\n\nDELIVERY: write the message for the team to "
            f"/workspace/{deliver.outbox_path(outcome_id)} (Markdown, concise — what changed "
            f"and what matters). Do NOT post, message or email it yourself: after independent "
            f"verification it is delivered once to: {where}. If verification fails, nothing "
            f"is sent.")
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
        + delivery
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
    oid = run.get("outcome_id")
    if oid:   # already attached (a recovery retry or a dry run): use THAT outcome's spec
        o = await db.get_outcome(oid)
        spec = await db.get_spec(o["spec_id"]) if o and o.get("spec_id") else None
        return prompt_addendum(spec, oid) if spec and spec["checks"] else None
    spec = await db.get_published_spec(agent["id"])
    if not spec or not spec["checks"]:
        return None
    oid = await db.create_outcome(agent["id"], agent["org_id"], spec, trigger="run",
                                  started_at=run.get("created_at"))
    await db.link_run_to_outcome(run_id, oid)
    return prompt_addendum(spec, oid)


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


async def verify_outcome(outcome_id: int, attempt: Optional[int] = None,
                         recover: bool = True) -> dict:
    """Run the spec's checks for an outcome, store results + receipt, set status.

    If the outcome isn't delivered and `recover` is set, diagnose the failure and
    either schedule another attempt, ask the user, or give up (step 2). Backfills
    and manual re-verifies pass recover=False: a new run today can't fix the past.
    """
    o = await db.get_outcome(outcome_id)
    spec = await db.get_spec(o["spec_id"]) if o and o.get("spec_id") else None
    if not o or not spec:
        raise ValueError("outcome or its spec not found")
    attempt = attempt or o["attempts"] or 1
    prev_status = o["status"]
    if o.get("trigger") in ("backfill", "dry_run"):
        recover = False          # one attempt only; a dry run never retries or delivers
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
    manifest = parse_manifest(run and run.get("result"))
    now = _now()
    cost = await db.outcome_cost(outcome_id)

    preview = None
    if o.get("trigger") == "dry_run" and deliver.message_destinations(spec):
        try:
            agent_ = await db.get_agent(o["agent_id"])
            msg = await deliver._read_outbox(o["agent_id"], outcome_id)
            preview = deliver.compose({**o, "manifest": manifest, "receipt": {}}, agent_ or {}, msg)["plain"]
        except Exception:  # noqa: BLE001
            log.exception("could not build the dry-run delivery preview")
    if delivered:
        receipt = _receipt(o, spec, results, run, True, strength)
        receipt["attempt_log"] = o["recovery"]
        if preview:
            receipt["delivery_preview"] = preview
        await db.update_outcome(
            outcome_id, status="delivered", finished_at=now, verified_at=now,
            next_attempt_at=None, needs_you_reason=None,
            receipt_json=json.dumps(receipt), cost_usd=cost,
            manifest_json=json.dumps(manifest) if manifest else None)
        # step 3: only now — verified — does anything reach the team (exactly once)
        try:
            await deliver.deliver_outcome(outcome_id)
        except Exception:  # noqa: BLE001
            log.exception("delivery failed for outcome %s", outcome_id)
        return await db.get_outcome(outcome_id)

    receipt = _receipt(o, spec, results, run, False, strength)
    if preview:
        receipt["delivery_preview"] = preview
    if not recover:
        keep = prev_status if prev_status in ("needs_you", "recovering") else "failed"
        receipt["attempt_log"] = o["recovery"]
        await db.update_outcome(
            outcome_id, status=keep, finished_at=now if keep == "failed" else None,
            receipt_json=json.dumps(receipt), cost_usd=cost,
            manifest_json=json.dumps(manifest) if manifest else None)
        return await db.get_outcome(outcome_id)

    decision = await _decide(o, spec, run, results, manifest, cost)
    log_ = o["recovery"] + [decision["entry"]]
    receipt.update(decision["receipt"])
    receipt["attempt_log"] = log_
    await db.update_outcome(
        outcome_id, status=decision["status"],
        finished_at=now if decision["status"] in ("failed", "needs_you") else None,
        next_attempt_at=decision.get("next_attempt_at"),
        needs_you_reason=decision.get("needs_you_reason"),
        recovery_json=json.dumps(log_), receipt_json=json.dumps(receipt), cost_usd=cost,
        manifest_json=json.dumps(manifest) if manifest else None)

    if decision["status"] in ("failed", "needs_you"):
        agent = await db.get_agent(o["agent_id"])
        name = (agent or {}).get("name", "routine")
        title = (f"Needs you: {name}" if decision["status"] == "needs_you"
                 else f"Outcome not delivered: {name}")
        try:
            await db.add_notification(o["org_id"], o["agent_id"], run and run.get("id"),
                                      title, receipt["summary"])
        except Exception:  # noqa: BLE001
            log.exception("could not add outcome notification")
    return await db.get_outcome(outcome_id)


# ---------------- recovery (step 2) ----------------

DEFAULT_MAX_ATTEMPTS = 3
RECOVERY_TICK_S = 20

_RX = {
    "auth": re.compile(r"\b401\b|unauthori[sz]ed|invalid (?:api )?key|authentication failed|"
                       r"token (?:has )?expired|reconnect|re-?authori[sz]e", re.I),
    "provider_limit": re.compile(r"\b429\b|rate.?limit|usage limit|quota|too many requests|"
                                 r"reached your [a-z ]*limit", re.I),
    "provider_error": re.compile(r"\b(?:500|502|503|504|529)\b|overloaded|service unavailable|"
                                 r"internal server error|bad gateway|api error", re.I),
}

LABELS = {
    "blocked": "Agent reported it was blocked",
    "auth": "A connected account or credential was rejected",
    "provider_limit": "Hit the model provider's usage limit",
    "provider_error": "The model provider returned an error",
    "oom": "Ran out of memory",
    "timeout": "Hit the time limit",
    "interrupted": "Interrupted by a server restart",
    "check_failed": "Finished, but verification failed",
    "run_failed": "The run failed",
}

# seconds to wait before the next attempt, by how many times this class recurred
_DELAYS = {"provider_limit": [600, 1800], "provider_error": [120, 600],
           "oom": [30], "timeout": [30], "interrupted": [15],
           "check_failed": [30], "run_failed": [60]}


def classify(run: Optional[dict], manifest: Optional[dict]) -> tuple[str, str]:
    """Why didn't this attempt deliver? Returns (class, short detail)."""
    if manifest and manifest.get("blocked"):
        return "blocked", str(manifest["blocked"])[:300]
    if not run:
        return "run_failed", "no run was found for this attempt"
    err = run.get("error") or ""
    status = run.get("status")
    if status == "succeeded":
        return "check_failed", ""
    if status == "interrupted":
        return "interrupted", err[:200]
    blob = f"{err}\n{run.get('result') or ''}"
    if err.startswith("out of memory") or "likely out of memory" in err:
        return "oom", err[:200]
    if err.startswith("timed out after"):
        return "timeout", err[:200]
    if _RX["provider_limit"].search(blob):
        return "provider_limit", blob.strip()[:200]
    if _RX["auth"].search(blob):
        return "auth", blob.strip()[:200]
    if _RX["provider_error"].search(err):
        return "provider_error", err[:200]
    return "run_failed", err[:300] or f"exit {run.get('exit_code')}"


def recovery_note(cls: str, attempt: int, max_attempts: int, detail: str,
                  results: list[dict], run: Optional[dict], memory: Optional[str]) -> str:
    """What the agent is told on the next attempt."""
    head = f"RECOVERY — this is attempt {attempt} of {max_attempts}. "
    if cls == "timeout":
        secs = _secs(run and run.get("started_at"), run and run.get("finished_at"))
        return head + (f"The previous attempt was stopped at the time limit"
                       f"{f' after {secs // 60} minutes' if secs else ''}. Check what already "
                       "completed and finish the rest instead of redoing everything.")
    if cls == "check_failed":
        bad = [r for r in results if r["required"] and not r["passed"]]
        lines = "\n".join(f"- {r['name']}: observed {r.get('observed') or '—'}, "
                          f"expected {r.get('expected') or '—'}" for r in bad)
        return head + ("The previous attempt finished, but independent verification "
                       f"failed:\n{lines}\nFind out why and make these checks pass.")
    if cls == "oom":
        return head + (f"The previous attempt ran out of memory. This attempt has "
                       f"{memory}. Avoid loading large data into memory all at once.")
    if cls in ("provider_limit", "provider_error"):
        return head + ("The previous attempt failed because the model provider returned an "
                       "error. Retry the task.")
    if cls == "interrupted":
        return head + "The previous attempt was interrupted by a server restart. Start again."
    return head + f"The previous attempt failed: {detail}. Investigate the cause and retry."


def _iso_in(seconds: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _decide(o: dict, spec: dict, run: Optional[dict], results: list[dict],
                  manifest: Optional[dict], cost: float) -> dict:
    """Pick the next step for an attempt that didn't deliver."""
    budget = spec.get("budget") or {}
    max_attempts = int(budget.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
    max_cost = budget.get("max_cost_usd")
    attempt = o["attempts"] or 1
    cls, detail = classify(run, manifest)
    label = LABELS[cls]
    entry = {"attempt": attempt, "class": cls, "label": label, "detail": detail,
             "run_id": run and run.get("id"), "at": _now()}

    def needs_you(reason: str) -> dict:
        entry["action"] = "needs_you"
        return {"status": "needs_you", "needs_you_reason": reason, "entry": entry,
                "receipt": {"headline": "Needs you", "status": "needs_you", "summary": reason}}

    def give_up(why: str) -> dict:
        entry["action"] = "gave_up"
        return {"status": "failed", "entry": entry,
                "receipt": {"headline": "Not delivered", "status": "failed",
                            "summary": f"Gave up after {attempt} attempt"
                                       f"{'s' if attempt != 1 else ''}: {why}"}}

    if cls == "blocked":
        return needs_you(f"The agent is blocked: {detail}")
    if cls == "auth":
        return needs_you("A connected account or credential was rejected — reconnect it, "
                         f"then retry. ({detail[:120]})")
    if attempt >= max_attempts:
        return give_up(f"{label.lower()} (budget: {max_attempts} attempts)")
    if max_cost is not None and cost >= float(max_cost):
        return give_up(f"cost budget of ${float(max_cost):.2f} reached")
    previous = [e for e in o["recovery"] if e.get("class") == cls]
    if cls == "run_failed" and previous:
        return needs_you(f"The run failed twice for an unexpected reason: {detail[:200]}")

    memory = None
    if cls == "oom":
        cur = (run and run.get("memory")) or None
        memory = config.step_memory(cur)
        if not memory:
            return needs_you(f"The job ran out of memory even at the "
                             f"{config.MAX_TASK_MEMORY} limit — it needs to use less memory.")
    delays = _DELAYS.get(cls, [60])
    wait = delays[min(len(previous), len(delays) - 1)]
    nxt = _iso_in(wait)
    entry.update({"action": "retry", "next_attempt_at": nxt, "memory": memory,
                  "note": recovery_note(cls, attempt + 1, max_attempts, detail, results,
                                        run, memory)})
    when = f"in {wait // 60} min" if wait >= 60 else "shortly"
    return {"status": "recovering", "next_attempt_at": nxt, "entry": entry,
            "receipt": {"headline": "Recovering", "status": "recovering",
                        "summary": f"Attempt {attempt}: {label.lower()}. Retrying {when} "
                                   f"(attempt {attempt + 1} of {max_attempts})."}}


async def _launch_next_attempt(o: dict) -> None:
    """Start the scheduled retry for a recovering outcome."""
    from . import runner   # lazy: runner imports this module
    agent = await db.get_agent(o["agent_id"])
    if not agent or not agent.get("enabled"):
        await db.update_outcome(o["id"], status="failed", finished_at=_now(),
                                next_attempt_at=None)
        return
    last = (o["recovery"] or [{}])[-1]
    prompt = (agent.get("prompt") or "")
    if last.get("note"):
        prompt = f"{prompt}\n\n{last['note']}"
    run_id = await db.create_run(agent["id"], prompt=prompt, memory=last.get("memory"))
    await db.link_run_to_outcome(run_id, o["id"])
    await db.update_outcome(o["id"], status="running", next_attempt_at=None)
    await runner.start_run(run_id, agent)


async def recovery_loop() -> None:
    """Background loop: launch retries whose time has come. Survives restarts
    because the schedule lives in the database, not in memory."""
    while True:
        try:
            for o in await db.due_recoveries():
                try:
                    await _launch_next_attempt(o)
                except Exception:  # noqa: BLE001
                    log.exception("could not launch recovery for outcome %s", o["id"])
        except Exception:  # noqa: BLE001
            log.exception("recovery loop tick failed")
        try:
            await deliver.retry_due()                  # failed deliveries due a retry
        except Exception:  # noqa: BLE001
            log.exception("delivery retry tick failed")
        await asyncio.sleep(RECOVERY_TICK_S)


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
    await verify_outcome(oid, recover=False)
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


# ---------------- needs-you actions (step 4) ----------------

async def resolve(outcome_id: int, action: str, message: Optional[str] = None) -> dict:
    """Answer a needs-you outcome: retry it, retry with the user's answer, or dismiss."""
    o = await db.get_outcome(outcome_id)
    if not o or o["status"] != "needs_you":
        raise ValueError("this outcome isn't waiting for you")
    now = _now()
    if action == "dismiss":
        log_ = o["recovery"] + [{"attempt": o["attempts"], "class": "user",
                                 "label": "Dismissed by you", "action": "dismissed", "at": now}]
        await db.update_outcome(outcome_id, status="failed", finished_at=now,
                                recovery_json=json.dumps(log_))
        return await db.get_outcome(outcome_id)
    if action == "answer" and not (message or "").strip():
        raise ValueError("write an answer for the agent")
    reason = o.get("needs_you_reason") or "a problem the agent couldn't fix on its own"
    note = ("RECOVERY — a person reviewed this job. "
            + (f"Their answer: {message.strip()}" if action == "answer" and message
               else "They asked you to try again.")
            + f"\nWhat went wrong before: {reason}")
    last_mem = next((r.get("memory") for r in reversed(o["runs"]) if r.get("memory")), None)
    entry = {"attempt": o["attempts"], "class": "user",
             "label": "You answered" if action == "answer" else "You asked to retry",
             "detail": (message or "")[:300], "action": "retry", "note": note,
             "memory": last_mem, "next_attempt_at": now, "at": now}
    await db.update_outcome(outcome_id, status="recovering", next_attempt_at=now,
                            needs_you_reason=None, finished_at=None,
                            recovery_json=json.dumps(o["recovery"] + [entry]))
    await _launch_next_attempt(await db.get_outcome(outcome_id))
    return await db.get_outcome(outcome_id)

# Feature: Outcome Jobs (delegate the job, get the outcome)

> Status: **steps 1–2 implemented** (2026-10-25).
> Step 1 — job specs, outcomes, independent `sql` + `artifact` verification,
> receipts, console outcome card; live on agent 60 (first live verified outcome:
> #3, 2026-10-25). Step 2 — recovery loop: failure classifier (blocked, auth,
> provider limit/error, OOM, timeout, interrupted, check failed, run failed),
> retries within budget (default 3 attempts, optional cost cap) with the failure
> fed back to the agent, OOM memory step-up (2g→3g→4g), provider back-off,
> needs-you for blocked/auth, DB-backed retry schedule, no overlapping scheduled
> runs, per-run failure alerts suppressed for attempts.
> Not yet: resuming a timed-out session (the harness only reports the session id
> on completion), provider *fallback* (only back-off), `connector` checks.
> Step 3 — produce → verify → deliver: agent stages its message in
> `outbox/outcome-<id>.md` and is told not to send; after verification the engine
> delivers exactly once per destination (in-app, Slack, Gmail/Outlook email,
> webhook json/slack) as the routine owner, retries a failed destination 3× then
> notifies once; webhooks are SSRF-guarded; backfills never deliver. Slack/email
> sends are not yet exercised live (no connected Slack/mailbox on the pilot account).
> Steps 4–6 not started.
> Positioning decision behind it: see `positioning` memory — "outcome-based, not
> ask-and-it-does".

## Summary

Today the unit of work in Antonagents is a **prompt** (or a prompt on a schedule).
A run ends when the model says it's done, and "succeeded" means the container
exited 0. Nothing checks that the work actually happened.

This feature changes the unit of work to a **job with a definition of done**. A
user delegates a job; the product owns it until the outcome is **produced,
verified, and delivered**, recovering from failures on its own and asking the user
only when it is truly blocked. "Succeeded" stops meaning "the agent stopped" and
starts meaning "a check confirmed the outcome exists where it should".

Chat stays. It becomes the way to **define** a job and to **answer escalations**,
not the product itself.

## Task-based vs outcome-based

| | Today (task-based) | Outcome-based |
|---|---|---|
| Unit of work | prompt + optional schedule | **job spec** with acceptance criteria |
| "Done" means | container exited 0 | **checks passed** against the spec |
| Failure | run marked failed, user notices (or not) | **recovery loop** retries differently, within a budget |
| User involvement | chat back-and-forth | **one clear question**, only when blocked or approval is needed |
| Result | transcript in the console | outcome **delivered into the user's tools** + a **receipt** |
| Metric | runs, minutes, cost | **verified outcomes**, cost and value per outcome |

## Concepts

- **Job spec**: the contract for a job. Objective, acceptance checks, where the
  result must land, constraints, budget, cadence. Versioned (draft → published),
  reusing the versioned-agent-config lifecycle from the roadmap.
- **Agent**: unchanged. It stays the *worker* (connectors, skills, model, data
  sources). A job spec is attached to an agent.
- **Outcome**: one occurrence of a job, created by a schedule fire, a manual
  delegation, or an API call. This is the thing the user cares about. It has a
  status, attempts, check results, and a receipt.
- **Attempt**: one run toward an outcome. An outcome may take several attempts.
  Existing `runs` rows become attempts.
- **Check**: one acceptance criterion, evaluated *independently* of the agent.
- **Escalation**: a single question to the user when the job is blocked or needs
  approval.
- **Receipt**: proof of delivery: what was done, where it landed, and the check
  evidence.

## The job spec

```yaml
objective: >
  Refresh competitor prices for ios.net.au and flag meaningful changes.
done_when:                       # acceptance checks (see Verification)
  - sql:    "SELECT count(*) FROM competitor_prices WHERE run_date = CURRENT_DATE"
    expect: ">= 14000"
  - connector: slack.message_posted
    target:  "#pricing"
    since:   outcome_start
deliver_to:
  - slack: "#pricing"            # summary of changes > 5%
  - postgres: competitor_prices  # the data itself (upsert keyed by sku+run_date)
constraints:
  - never delete rows from competitor_prices
  - approval_required: [send_email_external]
budget:
  max_attempts: 3
  max_runtime: 2h                # maps to agents.timeout_secs
  max_cost_usd: 15
cadence: "0 9 * * *"             # existing scheduler
escalate_to: owner               # who answers "needs you"
```

Fields:
- **objective**: plain-language outcome statement. Shown to the user and given to
  the agent.
- **done_when**: one or more checks; all `required` checks must pass.
- **deliver_to**: destinations via connectors / data sources, with format.
- **constraints**: hard rules and approval-gated actions.
- **budget**: attempts, runtime, cost. The recovery loop never exceeds it.
- **cadence**: existing schedule; `once` for one-off delegations.

Users don't write this YAML. The planner drafts it from a plain-language
description (see Authoring); the user reviews it as a form.

## Lifecycle

```
Draft spec ─► Published
                  │  (schedule fire / delegate / API)
                  ▼
              Outcome: Running ─► Verifying ─┬─► Delivered  (receipt)
                  ▲                          │
                  └──── Recovering ◄─────────┤  check failed or recoverable error
                                             ├─► Needs you  (blocked / approval)
                                             └─► Failed     (budget exhausted)
```

Outcome statuses: `running · verifying · recovering · delivered · needs_you · failed`.
A `needs_you` outcome resumes from where it stopped once the user answers.

## Verification

Verification is the heart of the feature. Rules:

1. **Independent of the agent.** The agent's claim is never the proof. Checks run
   host-side (or as a separate model call for judges), using **read-only**
   credentials.
2. **Deterministic first.** Prefer checks with a factual answer. Use judges only for
   qualitative outcomes, and label them honestly.
3. **The agent emits a delivery manifest.** At the end of each attempt the agent
   outputs structured JSON (what it produced, where, identifiers). Checks confirm
   the manifest; they don't take it on faith.

### Check types

| Kind | Example | Strength |
|---|---|---|
| `sql` | row count / freshness / value bounds on an attached data source | Verified |
| `artifact` | file exists in workspace, non-empty, N rows, matches schema | Verified |
| `connector` | Slack message posted, email sent, Linear issue created (read via Composio) | Verified |
| `http` | URL returns 200 / contains text | Verified |
| `schema` | final output validates against a JSON schema (e.g. report has required sections) | Verified |
| `judge` | LLM rubric with a score threshold, given the spec and the output | **Judged** |
| `human` | owner approves, or a sampled spot-check | **Accepted** |

The receipt and the ledger always show the strength: **Verified**, **Judged**, or
**Accepted**. We never present a judged outcome as verified.

## Recovery

When an attempt fails or a check fails, the engine classifies the failure and picks
an action, within the budget. Most classes are already detected today:

| Failure class | Detected by | Action |
|---|---|---|
| Timeout (exit 124) | runner, per-agent `timeout_secs` | resume the session with "continue from where you stopped"; if repeated, raise runtime within budget |
| Out of memory | OOM watcher (`oom` event) | retry with more memory (needs per-agent memory override) |
| Provider limit / outage (429, 5xx) | provider-error surfacing (to build) | back off; fall back to another provider |
| Connector auth (401, expired) | connector status | **needs you**: reconnect (can't self-fix) |
| Check failed | verifier | retry with the failure as context ("only 9,800 rows; expected ≥ 14,000") |
| Agent reports blocked | manifest `blocked` field | **needs you** with the agent's question |
| Unknown error | everything else | one retry, then **needs you** |

### No duplicate side effects

Retries must not spam people or corrupt data. Two rules:

- **Produce, verify, then deliver.** Where delivery is a message or an external
  action (Slack post, email), the agent produces into staging (workspace artifact,
  staging table) and the **delivery step runs once, after verification passes**.
- **Idempotent writes.** Where the side effect *is* the outcome (DB rows), the spec
  requires idempotent operations (upsert on a natural key). The planner adds this
  to the agent's instructions.

Approval-gated actions (external email, payments, deletes) pause the outcome in
`needs_you` with a preview; nothing executes until approved.

## Escalation ("Needs you")

- One escalation = one clear question, the context needed to answer it, and explicit
  options: approve · reject · provide input · reconnect · edit spec · retry now.
- Delivered through the existing `notifications` table and alert channels, plus a
  **Needs you** inbox with a badge in the nav.
- Answering resumes the same outcome; it does not start over.
- An unanswered escalation keeps that outcome blocked. Later occurrences of the job
  still run (configurable).

## Delivery and receipts

After verification passes, the engine runs the delivery step and generates a
receipt:

> **Delivered · Verified 09:52 IST** — Daily pricing pipeline
> 14,507 prices updated in `competitor_prices` (check: 14,507 ≥ 14,000 ✓).
> 12 changes > 5% posted to #pricing (check: message found ✓).
> 1 attempt · 51 min · $4.10

Receipts are stored on the outcome, shown in the UI, and optionally posted to a
`deliver_to` channel.

## Outcome ledger

Replaces the "impact" strip as the home view:

- Outcomes this period by status; **% verified-delivered without a human**
- Time saved = `baseline_minutes × delivered outcomes` (existing impact baseline)
- **Cost per verified outcome** (ties to the observability plan's outcome cost)
- Open escalations and the top recurring blockers

## Authoring: "Delegate a job"

1. User describes the outcome in plain language ("every morning, refresh competitor
   prices and tell #pricing what changed").
2. The **planner** (an LLM call with the agent's connectors and data sources in
   context) drafts the spec: objective, deterministic checks it can actually run
   against attached sources, delivery, budget, cadence.
3. The user reviews it as a form (checks in plain language, SQL behind a
   "details" toggle) and edits.
4. **Dry run**: one attempt + verification, no delivery. The user sees the receipt
   they would get.
5. Publish → spec v1 goes live.

A chat that already did the work can be turned into a job: **Save as job** proposes
the spec from the conversation (successor to today's "Save as routine").

## Data model (sketch)

```
job_specs(id, agent_id, version, status,           -- draft | published
          objective, deliver_to_json, constraints_json, budget_json,
          created_by, created_at)
job_checks(id, spec_id, kind, name, config_json,
           strength,                                -- verified | judged | accepted
           required)
outcomes(id, agent_id, spec_id, spec_version,
         trigger,                                   -- schedule | manual | api
         status, attempts, started_at, finished_at,
         manifest_json, receipt_json, cost_usd,
         needs_you_reason, created_at)
check_results(id, outcome_id, check_id, attempt, passed,
              observed, detail, created_at)
escalations(id, outcome_id, kind, question, options_json,
            status,                                 -- open | resolved
            resolved_by, resolution_json, created_at)

runs:  + outcome_id, + attempt                      -- a run becomes an attempt
```

**Backward compatible:** an agent without a published spec behaves exactly as today
(task mode). Chats stay task mode.

## Maps onto what exists

| Need | Already have |
|---|---|
| Worker config | agents + connectors + skills + data sources |
| Attempts | `runs` + `events` (add `outcome_id`, `attempt`) |
| Scheduling | `app/scheduler.py` (fires outcomes instead of bare runs when a spec is published) |
| Hook point | `app/runner.py` calls `alerts.evaluate_run(run_id)` after each run → the outcome engine goes here |
| Failure classes | timeout (124, `timeout_secs`), OOM watcher, exit codes |
| Notifications | `notifications` + `alert_rules` |
| Value baseline | `agents.baseline_minutes` |
| Read-only data access | `db_connections` (read-only mode) |
| Isolation | Docker per run (retries are safe to sandbox) |

New components:
- **Outcome engine** (`app/outcomes.py`): the loop: attempt → classify → verify →
  recover / deliver / escalate.
- **Verifier** (`app/verify.py`): check runners per kind.
- **Planner**: spec drafting from a description or a chat.
- **Manifest** in the harness contract: the adapter emits a final `manifest` event.
  Harness-agnostic (fits the planned harness adapters).

## API (sketch)

```
POST /api/agents/{id}/spec                 create/update draft spec
POST /api/agents/{id}/spec/draft           planner: description or chat → draft spec
POST /api/agents/{id}/spec/dry-run         one attempt + verify, no delivery
POST /api/agents/{id}/spec/publish
GET  /api/outcomes?status=&days=           ledger + list
GET  /api/outcomes/{id}                    attempts, check results, receipt
POST /api/outcomes/{id}/retry
GET  /api/escalations?status=open          Needs-you inbox
POST /api/escalations/{id}/resolve         approve / reject / input / reconnect
POST /v1/agents/{public_id}/outcomes       delegate via API (returns outcome id)
```

## UI

- Primary action: **Delegate a job** (chat remains for ad-hoc work).
- Job detail: spec (objective, checks, delivery, budget), latest receipt, outcome
  history.
- **Outcomes** view = the ledger, with status pills: Delivered · Verified /
  Delivered · Judged / Needs you / Recovering / Failed.
- **Needs you** inbox with a nav badge (also surfaced in the collapsed rail).
- Receipts render as cards; check evidence is expandable.

## Worked examples

**Agent 60, Daily pricing pipeline (fully checkable).** Spec as in the YAML above.
Typical day: 1 attempt, both checks pass, receipt posted. On a slow day the attempt
times out at 2h → engine resumes the session to continue → row-count check passes
→ delivered on attempt 2, no human involved. Today that day would have been a
silent "failed".

**Agent 81, Morning Ops Briefing (partly checkable).** Checks: `connector` (briefing
posted to the channel before 08:30) = Verified; `judge` (covers incidents,
deployments and open alerts from the last 24h; score ≥ 0.8) = Judged. Receipt says
**Delivered · Judged**.

**Agent 77, demand research (qualitative).** Only `schema` (brief has
problem/evidence/source sections, ≥ 10 sourced items) + `judge`. Shown as Judged,
with an optional weekly `human` spot-check. Not presented as verified.

## Build sequence

1. **Spec + verification on routines.** `job_specs`, `job_checks`, `outcomes`,
   `check_results`; `sql` / `artifact` / `connector` checks; manifest event;
   "Delivered · Verified" vs "Ran". Pilot: agent 60. *Biggest jump: changes what
   success means.*
2. **Recovery loop.** Failure classifier + actions for timeout, check-failed,
   unknown. Then OOM (needs per-agent memory override) and provider fallback
   (needs provider-error surfacing). Budgets enforced.
3. **Produce/deliver split + receipts.** Staging, single delivery step, receipt
   generation and posting.
4. **Delegate-a-job UX + Needs-you inbox.** Planner, form review, dry run,
   escalations.
5. **Outcome ledger.** Replace impact strip; outcome-level observability; cost per
   verified outcome.
6. **Judge + human checks** for qualitative jobs.

Dependencies on the existing roadmap: versioned-agent-config lifecycle (spec
versions), provider-error surfacing, per-agent memory override, evals/scorers.

## How we'll know it works

- % of outcomes **verified-delivered without a human** (primary)
- Escalation rate, and median time an escalation stays open
- **False-delivered rate**: outcomes marked delivered that the user says are wrong
  (sampled). Must stay near zero or trust collapses.
- Time from delegation to first delivered outcome

## Open questions

1. Naming in the UI: "job", "outcome", or "delegation"?
2. How far do non-technical users go in authoring checks? (Proposed: the planner
   writes them; users edit in plain language.)
3. Default budget (attempts / runtime / cost) per job type?
4. Which model runs judges, and who pays for judge calls?
5. Should unanswered escalations block later occurrences of the same job?
6. Outcome-based pricing: in scope later, or never?

## Not in scope (v1)

- Breaking one job into sub-jobs across multiple agents
- Dependencies between jobs
- Claiming outcomes for jobs with no checks (they stay task mode)
- Pricing changes

## Risks

- **Weak checks → false "verified".** Mitigation: planner proposes strong
  deterministic checks; dry run shows them; false-delivered rate is a tracked metric.
- **Retries duplicate side effects.** Mitigation: produce/verify/deliver split +
  idempotent writes.
- **Cost blowups from retries.** Mitigation: hard budgets per outcome.
- **Authoring friction.** Mitigation: planner + templates + "Save as job" from chat.

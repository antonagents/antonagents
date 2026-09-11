---
title: Observability pillar — accurate cost, cost decomposition, outcome cost, and audit hardening
date: 2026-09-11
status: ready-to-resume
owner: trysuperagent
artifact_readiness: implementation-ready
---

# Observability pillar: what's shipped, what's next

Pick this up when we resume observability. Part of the "agents as governed org
members" thesis: **access** (what an agent can do) · **memory** (what it knows) ·
**observability** (what it did, how well, at what cost).

## Already shipped (commit 0e67a95, live)

Admin-only **Observability** view + backend (`app/db.py`, `app/main.py`,
`web/console.html`):
- `db.ops_health(org, since)` — run volume, status mix, success rate, latency
  p50/p95/max (from run timing), cost, recent failures, per-agent rollup.
- `db.activity(org, since, ...)` + `_classify_activity` — the audit feed:
  tool_use events classified into shell / web / files / task / skill / **app
  (connector)** / **data (data-source access)**. DB access via
  `psql|mysql|SUPERAGENT_DB_*` is surfaced as its own `data` category.
- `GET /api/observability/health` and `/activity` (require_admin, org-scoped,
  7/30/90-day window). Console: stat cards, per-agent table, filterable activity
  audit with category chips + CSV export.

## Known gaps (the "next" work)

1. **Cost is wrong.** The Cost card is labelled `provider-approx` because
   `runs.cost_usd` comes from the SDK, which prices everything at Anthropic
   rates — wrong for GLM/gateway models. This is the #1 thing to fix; it also
   unblocks metering/billing.
2. **Audit is heuristic, not guaranteed.** We *observe* data access by
   regex-matching Bash (`psql`/`SUPERAGENT_DB_*`); an agent reaching data another
   way (python script, ORM, curl, exported file) is missed/miscategorised. The
   real fix is **mediated access** (route data/tool access through a logged
   gateway) — that belongs with the ACCESS pillar, not here.
3. **No cost decomposition, no outcome cost, no anti-pattern detection.**

## Next build — accurate cost + the Uber "Software Factory" model

Reference: Uber's efficient-software-factory blog
(https://www.uber.com/us/en/blog/efficient-software-factory/, reviewed
2026-09-11). It's effectively a design doc for the cost half of observability.
Their thesis: **eliminate zero-value token waste** rather than downgrade models;
decouple usage growth from cost growth (they grew users 7×, held spend flat, cut
cost/1K requests 34%). Adopt these five things, in order:

### 1. Accurate per-provider cost (foundational — do first)
- Add a **usage × price table** per provider+model (input/output/cache token
  prices). Multiply by **real token counts** (the SDK/`result` event carries
  usage; capture input/output/cache tokens per run into `runs`, not just the
  SDK's Anthropic-priced dollar figure).
- Store per-run: `tokens_in`, `tokens_out`, `tokens_cache`, and a **computed**
  `cost_usd` from our price table (keyed by the run's `routed` provider/model).
- Replace the `provider-approx` label; the Cost card becomes real.
- This unblocks **metering → billing** (Cloud) and **per-team chargeback**
  (Enterprise).

### 2. Cost decomposition (don't just show a lump $)
Adopt Uber's equation so spend is explainable by lever:
`Spend = Users × Sessions/User × Turns/Session × Requests/Turn × Tokens/Request × Price/Token`
- Surface the decomposition in the Observability view (which lever is driving
  spend: adoption/engagement vs tokens/request vs price/token).
- Maps onto our runs/events data (runs = sessions, events = turns/requests).

### 3. Outcome-denominated cost (the sellable metric)
- Show **cost per successful outcome**: cost per succeeded routine run, per
  report/artifact delivered, per alert fired — not just cost per token.
- Pair with the existing **impact** view (minutes saved / value). "Cost per
  outcome + value per outcome" is the differentiator over raw token dashboards.

### 4. Anti-pattern detection dashboard (the "oversight" feature)
Uber flags 16 anti-patterns, each with **$ impact + a remediation step**. Start
with the cheap, high-signal ones we can detect from our data:
- **Model misrouting** — an expensive model on a trivial task (cost vs outcome).
- **Context bloat** — runs with huge token/request (e.g. the old Composio
  schema-injection problem we already fixed with the Tool Router; detect
  regressions).
- **Vision on no-vision model** — the image-poisoning failure class we found.
- **Repeated failures / retries** on the same agent.
Each finding: plain-language cause + estimated $ + how to fix. This is the
"oversight of a workforce" feature, not a log.

### 5. Benchmark-driven, Pareto model routing (later)
- Pick provider+model per task from real-work benchmarks (cost / quality /
  reliability), re-migrate as the frontier shifts. Our multi-provider routing
  should aspire to this rather than a static default.

## Notes / things already true in our favour
- Our **Composio Tool Router** fix (389K→6.8K tokens) is exactly Uber's
  "tool search + CLI replaces preloading 1000+ MCP schemas (50-70K→~0)" win —
  we already did the biggest token optimisation.
- We rely on the **CLI's auto-compaction** (verified firing on a 44-turn GLM
  chat); Uber compacts at a 400K threshold explicitly — worth surfacing context
  usage per session as an anti-pattern signal.
- Prompt-cache TTL strategy (Uber: 5-min subagent / 1-hr main) — informational;
  our runs are ephemeral containers so less directly applicable.

## Build sequence when we resume
1. Capture real token counts per run (`result` event → `runs.tokens_*`).
2. Usage × price table + computed `cost_usd`; fix the Cost card label. **(P1)**
3. Cost decomposition surface in the Observability view.
4. Outcome-denominated cost, paired with impact.
5. Anti-pattern dashboard (start with model-misrouting + context-bloat + vision).
6. (Later) benchmark-driven Pareto routing.

Sensitive commercial framing (why this sells, tier mapping) is kept in the
`roadmap-observability` memory, not here.

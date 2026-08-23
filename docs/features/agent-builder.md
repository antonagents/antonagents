# Feature: Agent Builder (Studio)

> Status: **spec / backlog** (captured 2026-07-17). Not yet implemented.

## Summary

Evolve Antonagents from a **one-shot task runner** into an **agent builder**: a user
signs in, works inside an org, and assembles a reusable **agent** from connectors +
skills. They then **chat** with it, put it on a **schedule (routine)**, and receive
**alerts**. Adds first-class **database connectors** (PostgreSQL, MySQL, MongoDB,
ClickHouse) alongside the existing SaaS connectors.

This reframes the product around a persistent, configured **agent** (a "worker"),
not a fire-and-forget task.

## The flow (as requested)

| # | Step | Today | What's new |
|---|---|---|---|
| 1 | **User log-in** | ✅ email/password + Google (`app/auth.py`) | — |
| 2 | **Org creation** ⚠️ | ❌ ownership is per-user only | Add an **org/workspace** layer above users (org → members → agents). *See open Q1.* |
| 3 | **Create agent → select connectors** | ⚠️ connectors exist but are per-user, not bound to an agent | An **Agent** entity that pins a chosen set of connectors (+ model, system prompt) |
| 4 | **Add skills** | ❌ | **Skills**: reusable capabilities attached to an agent (see below) |
| 5 | **Chat conversation** | ⚠️ session resume + follow-ups exist (`/api/tasks/{id}/reply`) | A real **interactive chat UI** with the agent, not just one-shot runs |
| 6 | **Schedule routine** | ✅ cron/interval scheduler (`app/scheduler.py`) | Per-**agent** routines + management UI |
| 7 | **Alerts** | ❌ | Notify the user on conditions/events (run failure, agent-detected condition, thresholds) over channels |
| + | **DB connectors** | ⚠️ image ships `psql`/`mysql`/`sqlite` clients; no managed creds | Native **DB connector** type: Postgres, MySQL, MongoDB, ClickHouse |

## Concepts introduced

- **Org**: a tenant/workspace owning members, agents, connectors, and billing. Sits
  above the current per-user `owner_id` model.
- **Agent**: a saved configuration = { connectors[], skills[], model/provider,
  system prompt, schedule?, alerts? }. A "task/run" becomes an *execution of an agent*.
- **Skill**: a reusable, attachable capability. Likely one (or a mix) of: a named
  instruction/prompt template, a bundle of allowed tools/MCP servers, or a saved
  routine. *See open Q2.*
- **Chat session**: a persistent conversation with an agent (built on the existing
  session-resume + persistent per-agent workspace).
- **Alert**: a rule that fires a notification on an event/condition.

## Database connectors (native — not Composio)

Composio is SaaS-OAuth oriented and does **not** cover raw database connections, so
DB connectors are a **native driver** behind the existing `Connector` interface
(`app/connectors/`), which is exactly why that interface was built swappable.

- Engines: **PostgreSQL, MySQL, MongoDB, ClickHouse**.
- Store the connection credential (URL/DSN) **encrypted at rest**, scoped to
  org/user/agent — do NOT put it in the task's plaintext `env_json`
  (see the plaintext-secrets debt already flagged).
- At run/chat time, inject the connection into the agent's container (the task image
  already has `psql`/`mysql`/`sqlite3`; **add `mongosh` + a ClickHouse client**).
- Read-only vs read-write toggle per connection (safety for autonomous runs).

## Data-model additions (sketch)

```
orgs(id, name, created_at)
org_members(org_id, user_id, role)
agents(id, org_id, owner_id, name, model, provider, system_prompt, created_at)
agent_connectors(agent_id, connector_ref)          -- SaaS + DB connections bound to an agent
skills(id, org_id, name, kind, definition_json)
agent_skills(agent_id, skill_id)
chat_sessions(id, agent_id, user_id, sdk_session_id, created_at)
chat_messages(id, session_id, role, content, run_id?, ts)
db_connections(id, org_id/owner_id, engine, dsn_encrypted, mode, created_at)
alert_rules(id, agent_id, trigger, condition_json, channel, target)
```

## Maps cleanly onto what exists

- Auth/multi-tenancy, per-run isolation, scheduling, streaming, session-resume,
  connector interface, event log — all reusable. The new work is mostly **modeling**
  (org, agent, skill, alert) + **UI** (builder, chat, alerts) + the **DB driver**.

## Open questions (decide before building)

1. **"Org creation"** — confirm this means **organization/workspace** (a tenant above
   users), vs. "user creation" (admin invites users). Assumed: org/workspace.
2. **Skills** — what is a skill, concretely? Prompt template / tool bundle / saved
   routine / all three? This drives the whole `skills` model.
3. **Alerts** — which triggers (run failure, schedule miss, agent-detected condition,
   metric threshold) and which channels (email, Slack, webhook, in-app)?
4. **Agent vs Task** — does "Agent" replace "Task", or is a Task a *run of an Agent*?
   (Recommend: Agent is the config; a run/chat-turn is its execution.)

## Not in scope of this note

Capture only — no implementation yet. Sequence and build after the open questions are
answered and the GTM beachhead workflow is locked.

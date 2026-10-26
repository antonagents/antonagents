# Security

This document describes Antonagents's security model, its known trade-offs, and how
to report vulnerabilities. Antonagents is designed for **self-hosting by a team that
trusts its own users** (an organization running it on its own infrastructure). Read
the caveats below before exposing it to untrusted users.

## Reporting a vulnerability

Please report security issues privately — do **not** open a public issue. Email the
maintainers (see the repository owner's profile) with details and reproduction
steps. We'll acknowledge and work on a fix before any public disclosure.

## Trust model

- **Tenancy.** Data is scoped to an **org**. Users belong to one or more orgs
  (memberships) with an Admin/Member role; agents, runs, artifacts, and data
  sources are visible only within their org.
- **Intended deployment.** A single organization self-hosting for its own members.
  Multi-org support exists, but the execution and secret-handling model below
  assumes members are **trusted**. Do not run a public/untrusted-signup instance
  without the hardening noted under "Known limitations."

## Secrets at rest

- **Data-source DSNs** and **agent `env`** (any tokens pasted into a routine) are
  **encrypted at rest** with Fernet, keyed off `SUPERAGENT_SESSION_SECRET`
  (`app/connectors/db_driver.py`, `app/db.py`). They are decrypted only at run
  launch and injected into the task container as environment variables.
- **`SUPERAGENT_SESSION_SECRET` is durable and must be backed up.** It keys the
  encryption above (and signs session cookies). If it changes, all encrypted
  data-source credentials and agent env become **unrecoverable**. Set a stable,
  long, random value in production and store it safely.

## Connectors (SaaS integrations) and the action gateway

Connectors (Composio) are **disabled by default** — they only activate when you set
`COMPOSIO_API_KEY`.

Every connector call a run makes goes through the **action gateway**
(`app/gateway.py`), an internal listener on the task network's gateway address
(`SUPERAGENT_GATEWAY_HOST`/`_PORT`, default `<docker bridge>:8090`, never public):

- **The provider key never enters a task container.** A run gets a per-run URL
  (`/mcp/<random token>`) that only reaches its owner's connector session, only
  while the run is alive; the gateway adds the key server-side. (Previously the
  org-wide key was injected into containers — fixed 2026-10-26.) If the gateway
  can't start, runs get **no** connectors rather than the key.
- **Guardrails** (`app/guardrails.py`): each action is classified (read, write,
  message, destructive, money, code, admin) and the routine's policy decides
  *allow*, *approve* (held; a person approves and the platform runs it once,
  exactly as requested) or *block*. Routine defaults: messages, deletes and
  payments need approval; opaque code execution in the provider sandbox and
  connection changes are blocked. Chats allow everything (a person is present).
  Per-routine overrides and per-tool rules live in the console (Guardrails).
- **Audit**: every action is recorded in the `actions` table with its arguments,
  decision, result and who approved it.

What guardrails do **not** cover (enforce these elsewhere):

- **Database writes.** Data-source access is a DSN inside the container; the
  only real write protection is a SELECT-only database role (see Data sources).
- **Other credentials you give a routine** (env secrets, API keys) — code in the
  container can call those services directly.
- **General internet egress** from task containers (see Known limitations).

## Run execution & isolation

- Each run executes in an **ephemeral Docker container** (`--rm`), non-root, with
  memory / CPU / PID limits and a per-run wall-clock timeout.
- Containers use the `bridge` network with **full egress** by default. Restrict
  this with a locked-down Docker network if you need to control where tasks reach.
- **Docker socket.** When you run the app itself in a container (see
  `docker-compose.yml`), it mounts the host Docker socket so it can spawn task
  containers as siblings. This gives the app container **host-root-equivalent**
  privileges — inherent to the per-run execution model and acceptable for
  single-tenant self-hosting. Do not expose that app container to untrusted input
  paths beyond the application itself.

## Authentication

- Users log in with email/password or Google OAuth; the session is a signed cookie.
- Set `SUPERAGENT_COOKIE_SECURE=true` when serving over HTTPS.
- There is **no email verification or password reset** yet (no SMTP). Add these
  before enabling public self-signup.

## Known limitations (before untrusted multi-tenant)

These are acceptable for trusted self-hosting but must be addressed before running
an untrusted/public instance:

1. ~~Connector org-key injection into task containers~~ — fixed by the action gateway (2026-10-26).
2. Full-egress task network (abuse vector for untrusted users).
3. Single global encryption key (`SUPERAGENT_SESSION_SECRET`) rather than
   per-tenant keys / KMS.
4. No login rate-limiting, email verification, or password reset.
5. Shared single Docker daemon for all runs (blast radius / noisy neighbors).

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

## Connectors (SaaS integrations) — trusted-use only

Connectors (Composio) are **disabled by default** — they only activate when you set
`COMPOSIO_API_KEY`. **Leave them off unless every user on the instance is trusted.**

When enabled, the runner injects a per-user MCP server config into the task
container so the agent can act as that user. That config currently carries the
**org-wide Composio API key** as an `x-api-key` header
(`app/connectors/composio_driver.py`). Because a task container runs
agent-controlled code with shell and network access, a malicious or
prompt-injected task could read that key and reach **every** connected user's
accounts. The app logs a warning at startup when connectors are enabled.

**Mitigation / roadmap:** a host-side MCP proxy that holds the org key and injects
per-user scoping (so the key never enters a task container), or a Composio-issued
per-session scoped token. Until then, treat connectors as trusted-internal-use
only. This is tracked as a separate hardening track and is the gate before
onboarding untrusted external tenants.

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

1. Connector org-key injection into task containers (see above).
2. Full-egress task network (abuse vector for untrusted users).
3. Single global encryption key (`SUPERAGENT_SESSION_SECRET`) rather than
   per-tenant keys / KMS.
4. No login rate-limiting, email verification, or password reset.
5. Shared single Docker daemon for all runs (blast radius / noisy neighbors).

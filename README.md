# Antonagents

A self-hosted, multi-tenant agent platform. You create **agents** — persistent,
reusable configurations (model, instructions, schedule, connectors, data
sources) — and run them from a chat console or on a schedule. Each execution is
a **run**: the agent carries the work out end-to-end (coding, data/DB analysis,
research, automation) in its own ephemeral **Docker** container and streams
progress back live. Built on the **Claude Agent SDK**.

Use it from the web console at **`/app`** (Chat + Routines), or drive it over the
HTTP API. Everything is scoped to an **org** (team): agents, runs, and data
sources are shared within a team and invisible to everyone else.

```
Browser / API client
        │  POST /api/agents               (name + instructions + schedule + access)
        │  POST /api/agents/{id}/chat|run  ──► creates a run
        ▼
FastAPI service (app/)  ──create run──►  runner  ──docker run──►  ┌─ task container ─┐
   SQLite: orgs/agents/runs/events                                │ claude CLI +     │
   APScheduler: recurring agents          ◄── JSONL events ───────│ Agent SDK        │
   SSE: live streaming                                            │ (agent/run_task) │
                                                                  └──────────────────┘
```

## Why this shape

- **Agent SDK, self-hosted** — the SDK ships the full agent loop, built-in
  tools (shell/files/web), subagents, and long-run context management. We add
  the multi-user service, scheduling, and isolation around it.
- **One container per run** — runs are arbitrary and come from different agents
  and users, so each executes in a throwaway container (non-root, resource-
  limited, no host bind mounts — only its own workspace volume). That container
  is the security boundary; inside it the agent runs autonomously so it can
  actually finish the job.
- **Self-contained access** — each agent carries whatever access it needs (DB
  data sources, tokens) in its `env` / bound data sources, injected only into
  that agent's containers.

## Prerequisites

- **Docker** running on the host (Compose v2 for the quickstart below).
- At least one LLM provider key — e.g. an `ANTHROPIC_API_KEY` (see
  [Model routing](#model-routing-multiple-llm-providers) for alternatives).
- For the manual path only: Python 3.11+ and Node (Node is needed just to
  *build* the task image, which bundles its own Node + `@anthropic-ai/claude-code`).

## Quickstart (Docker Compose)

```bash
# 1. Build the isolated per-run task image (the agent sandbox). This is a
#    separate image the app launches on the host daemon — compose does not build it.
docker build -t superagent-task:latest -f docker/Dockerfile.task agent/

# 2. Configure
cp .env.example .env
#    Set a STABLE, random SUPERAGENT_SESSION_SECRET — it keys the at-rest
#    encryption of credentials, so if it changes, encrypted data is unrecoverable:
python3 -c "import secrets; print('SUPERAGENT_SESSION_SECRET='+secrets.token_urlsafe(48))" >> .env
#    Then edit .env and set your provider key (e.g. ANTHROPIC_API_KEY).

# 3. Run
docker compose up --build
# open http://localhost:8080/app
```

**Shortcut:** `make up` does the whole thing in one command — creates `.env`
(with a generated `SUPERAGENT_SESSION_SECRET`) if missing, builds the task image
if it isn't built yet, and starts the app. (Set your provider key in `.env`
first, or after — the app starts either way, but agent runs need a key.) Run
`make help` to see all targets.

The app container mounts the host Docker socket so it can launch per-run task
containers as siblings on the host daemon — see [Security](#security) for the
trade-off this implies.

## Manual setup (run on the host)

```bash
# 1. Build the task image (as above)
docker build -t superagent-task:latest -f docker/Dockerfile.task agent/

# 2. Install the service deps
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. Configure (set SUPERAGENT_SESSION_SECRET + a provider key)
cp .env.example .env
set -a && . ./.env && set +a

# 4. Run the service (use tmux/systemd so it survives disconnects)
python -m app.main
# open http://<server>:8080/app
```

## Auth & multi-tenancy

The API requires a logged-in user, and everything is scoped to an **org**
(team). Agents, runs, and data sources are **shared within your org** and
invisible to other orgs (a resource outside your active org returns 404). Users
can belong to multiple orgs (Admin/Member roles) and switch between them; invite
teammates from the Members view. Auth is by HttpOnly **session cookie**, so the
browser also carries it on the SSE stream automatically.

Two login methods:
- **Email + password** — always available (`/api/auth/register`, `/api/auth/login`).
- **Google (Gmail)** — enabled when `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are
  set (create an OAuth 2.0 Web client in Google Cloud, redirect URI
  `.../api/auth/google/callback`). If unset, the button is hidden.

## Follow-up questions (conversations)

An agent holds a conversation across runs. A chat turn —
`POST /api/agents/{id}/chat` with `{"prompt": "..."}` — starts a new run that
**resumes the agent's prior session** (full memory) and reuses its **persistent
workspace**, a per-agent Docker volume mounted at `/workspace`, so files the
agent wrote earlier are still there. That volume is an isolation boundary: it is
only ever mounted into that agent's containers and is deleted when the agent is
deleted. (Because the workspace persists across runs, recurring agents accumulate
state there.)

## Console API (session cookie)

The console at `/app` is driven by these endpoints. They need an authenticated
**session cookie** (log in first) and act in your active org. For **programmatic /
third‑party access**, use the [Programmatic API (`/v1`)](#programmatic-api-v1)
below instead — it uses API keys, not cookies.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/register` | Create an account (email/password) |
| POST | `/api/auth/login` / `/api/auth/logout` | Log in / out |
| GET | `/api/auth/me` | Current user (401 if not logged in) |
| GET | `/api/auth/google/login` | Begin Google OAuth (if configured) |
| POST | `/api/agents` | Create an agent (`run_now`, or `schedule_kind` = `cron`/`interval`) |
| GET | `/api/agents` | List your org's agents with their latest run |
| GET | `/api/agents/{id}` | Agent detail + run history |
| POST | `/api/agents/{id}/run` | Trigger a run now (uses the agent's default prompt) |
| POST | `/api/agents/{id}/chat` | Chat turn: a run whose prompt is your message, resuming the session |
| POST | `/api/agents/{id}/enable?enabled=false` | Pause/resume a schedule |
| DELETE | `/api/agents/{id}` | Delete an agent + its workspace |
| GET/POST | `/api/db-connections` | List / add read-only DB data sources (org-scoped) |
| GET | `/api/runs/{id}` | Run detail + all stored events |
| GET | `/api/runs/{id}/stream` | Live SSE event stream |

These calls need an authenticated session cookie (log in first); the console at
`/app` is the easy way to do the same things.

### Example: one-shot DB anomaly check (run immediately)

```bash
curl -s localhost:8080/api/agents -H 'content-type: application/json' -d '{
  "name": "DB anomaly scan",
  "prompt": "Connect to the database at $DATABASE_URL. Inspect the orders table for the last 24h and report any anomalies (spikes, nulls, dupes, out-of-range values). Summarize findings.",
  "env": {"DATABASE_URL": "postgres://user:pass@host:5432/db"},
  "schedule_kind": "once",
  "run_now": true
}'
```

### Example: nightly recurring agent

```bash
curl -s localhost:8080/api/agents -H 'content-type: application/json' -d '{
  "name": "Nightly report",
  "prompt": "Generate the daily metrics report and save it to ./artifacts/report.md",
  "schedule_kind": "cron",
  "schedule_expr": "0 3 * * *"
}'
```

## Programmatic API (`/v1`)

Call an agent from your own code — no login, no cookie. Each agent has a stable
**public ID** (`ag_…`) and you mint per‑agent **API keys** (`sk_ag_…`) to call it.
A key is scoped to exactly one agent and can be revoked at any time. Ideal for
embedding an agent in your product, wiring it into a workflow, or building on top.

### Get a key

In the console, open an agent (a chat or a routine) → **API** → **Create key**.
The raw key is shown **once** — copy it then; only a hash is stored. The panel also
shows the agent's public ID and a ready‑to‑run curl example. (Or via the console
API: `POST /api/agents/{id}/keys`.)

> **Keep keys server‑side.** A key can trigger runs (which cost model tokens) on
> its agent. Treat it like any secret — never ship it in a browser/client.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/agents/{public_id}/chat` | Send a message; returns a `run_id`. Resumes the agent's session. |
| POST | `/v1/agents/{public_id}/run` | Trigger the agent with its default instruction. |
| GET | `/v1/runs/{run_id}?public_id={public_id}` | Fetch a run's status, result, and events. |

Auth: `Authorization: Bearer sk_ag_…`. A key only works for **its own** agent —
using it against another agent returns `403`; a missing/invalid/revoked key is
`401`. POST bodies are JSON — always send `Content-Type: application/json` (curl's
`-d` defaults to form encoding, which yields a `422` "not a valid dictionary"
error).

### Example

```bash
# 1. send a message
curl https://your-host/v1/agents/ag_xxx/chat \
  -H "Authorization: Bearer sk_ag_..." \
  -H "Content-Type: application/json" \
  -d '{"message": "Summarize the latest orders and flag anything unusual."}'
# → { "run_id": 238, "agent_id": "ag_xxx" }

# 2. get the result (poll until status is succeeded/failed)
curl "https://your-host/v1/runs/238?public_id=ag_xxx" \
  -H "Authorization: Bearer sk_ag_..."
# → { "status": "succeeded", "result": "...", "events": [ ... ] }
```

The agent runs with everything it's configured with — its model, data sources,
connectors, skills, and persistent workspace/session — so an API call gets the
same capabilities as a chat in the console.

### Rate limits

`/v1` is rate‑limited **per key** (token bucket, default **120 requests/min**,
configurable via `SUPERAGENT_V1_RATE_PER_MIN` / `SUPERAGENT_V1_RATE_BURST`; `0`
disables it). Over the limit returns `429` with a `Retry-After` header. One key's
traffic never affects another's.

> **Note:** the API is designed for trusted / server‑side callers. Exposing agents
> to untrusted **end‑users** (e.g. an embedded chat widget) needs additional layers
> — end‑user session tokens and, for agents on multi‑tenant data, row‑level
> isolation enforced outside the agent. See [`docs/SECURITY.md`](docs/SECURITY.md).

## Model routing (multiple LLM providers)

**Provider keys are set once by the operator, in `.env` — they are instance-wide.**
Everyone who uses that instance shares the same provider key(s); end users do not
bring their own. (The only per-user credentials are **connectors** — Slack/Gmail
via OAuth.) Minimum to run: `SUPERAGENT_SESSION_SECRET` plus **one** provider key
(default provider is `anthropic`, so `ANTHROPIC_API_KEY`). Set `ZAI_API_KEY`,
`OLLAMA_API_KEY`, etc. and `SUPERAGENT_DEFAULT_PROVIDER` to use a different one;
`providers.json` lists all shipped providers and lets you add your own — including
a local Anthropic-compatible endpoint (Ollama / a LiteLLM gateway) to run with
**no external key at all**.

Each run executes in its own container, and the Agent SDK selects its backend via
env vars — so the **router picks a provider+model per run** and injects only
that provider's endpoint + key into that run's container. Setting exactly one
auth var per container keeps providers cleanly separated.

**Built-in providers** (shipped in `providers.json`):

| Provider | Endpoint | Key env | Example models |
|---|---|---|---|
| `anthropic` | native | `ANTHROPIC_API_KEY` | `claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5` |
| `zai` | `https://api.z.ai/api/anthropic` | `ZAI_API_KEY` | `glm-5.2`, `glm-4.7`, `glm-4.5-air` |
| `ollama` | `https://ollama.com` | `OLLAMA_API_KEY` | `glm-5.2:cloud`, `glm-5.1:cloud`, `glm-4.6:cloud` |
| `deepinfra` | `https://api.deepinfra.com/anthropic` | `DEEPINFRA_API_KEY` | `zai-org/GLM-5.2`, `zai-org/GLM-5.1` |
| `openai` | local gateway (`…:4000`) | `OPENAI_GATEWAY_KEY` | `gpt-5.6-sol` (frontier), `gpt-5`, `o3`, `o4-mini` — **reasoning models** (via LiteLLM; list fetched live from the gateway) |

Most of these work because they expose an **Anthropic-compatible** API — the SDK
talks to them unchanged. `openai` is the exception: OpenAI/Gemini aren't
protocol-compatible, so that entry points at a **local LiteLLM gateway** you run
(on the Docker bridge) which translates. Add any Anthropic-protocol endpoint the
same way in `providers.json`.

**How a run's model is chosen** (`app/router.py`), in order:
1. **Explicit** — the agent's `provider` / `model` fields (UI dropdowns or API).
2. **Rules** — keyword rules in an optional `routing.json`.
3. **Default** — `SUPERAGENT_DEFAULT_PROVIDER` + that provider's default model.

Example `routing.json` (route hard work to Claude, cheap/bulk work to GLM):
```json
[
  {"contains": ["migrate", "refactor", "architect", "security"], "provider": "anthropic", "model": "claude-opus-4-8"},
  {"contains": ["summarize", "classify", "format", "extract"],   "provider": "zai",       "model": "glm-4.5-air"},
  {"provider": "zai", "model": "glm-5.2"}
]
```

Add/replace providers wholesale with a `providers.json` (same fields as the
`Provider` dataclass). `GET /api/providers` lists what's configured.

> **Note on GLM/Z.ai:** the API routes to China-based infrastructure — consider
> that for tasks handling sensitive data. GLM-5.2 is MIT open-weights, so you
> can self-host it and point a provider entry at your own endpoint instead.

### Using OpenAI / other models via LiteLLM

The agent speaks the **Anthropic protocol**, so OpenAI/Gemini/Azure/etc. (which
aren't protocol-compatible) run through a **LiteLLM gateway** that translates.
LiteLLM ships as an **optional Compose profile** — off by default.

There are **two keys**, and they go in different places:

```
Antonagents ──(OPENAI_GATEWAY_KEY)──► LiteLLM gateway ──(OPENAI_API_KEY)──► OpenAI
```

- **`OPENAI_API_KEY`** — your real OpenAI key. Used **only by the gateway**;
  Antonagents and the task containers never see it.
- **`OPENAI_GATEWAY_KEY`** — a secret you choose. It's the gateway's master key
  **and** what Antonagents uses to authenticate to the gateway.

Setup:

```bash
cp litellm.config.example.yaml litellm.config.yaml   # edit to add models/vendors
# in .env: set OPENAI_API_KEY (real key) and OPENAI_GATEWAY_KEY (a secret you pick)
docker compose --profile litellm up                  # starts the app + the gateway
```

Then in an agent's model settings pick provider **"OpenAI (via LiteLLM gateway)"**
→ **`gpt-5`**, **`gpt-5-mini`**, **`o3`**, **`o4-mini`**.

> **OpenAI reasoning models only.** Antonagents's agent runs with extended thinking,
> which LiteLLM forwards to OpenAI as `reasoning_effort`. The GPT-5 family and the
> o-series accept it; **non-reasoning models (`gpt-4o`, `gpt-4.1`) reject it and
> error**, so list only reasoning models in `litellm.config.yaml`.

**The model dropdown is populated live from the gateway** (`/v1/models`), so it
always shows exactly the models you've configured in `litellm.config.yaml` — add a
new model there (or a whole new vendor like Gemini) and it appears in the UI, no
`providers.json` edit needed. When the gateway is unreachable, the dropdown falls
back to the static `models` list in `providers.json`.

The gateway binds to the Docker bridge gateway (`172.17.0.1:4000`) so per-run task
containers can reach it while it stays off the public internet. Because your model
key lives only in the gateway (infra you control), it never enters the agent
runtime — which fits the self-host / data-residency model.

## Connectors (SaaS integrations)

Users connect their own SaaS accounts (Slack, Linear, Gmail to start) so the
agent can act **as them**. Auth + tokens are handled by a managed connector
platform — **Composio** — behind a thin swappable interface (`app/connectors/`),
so a self-hostable driver (Nango) can be dropped in later without touching the
runner, API, or UI.

- **Enable**: set `COMPOSIO_API_KEY` in `.env`. A **Connections** panel appears
  in the console; users click Connect, complete OAuth, and the status flips to
  active. In dev, Composio auto-creates managed auth configs (no dashboard setup);
  for production/custom OAuth, set `COMPOSIO_AUTHCONFIG_*` ids.
- **How the agent gets the tools**: at run launch the runner asks the connector
  for the owner's per-user **MCP server** (scoped to their connected apps) and
  injects it into the container; the agent then has `mcp__composio__*` tools.
  Tokens stay in Composio — not in the task's `env` or our DB.

> **Security note:** connectors are **off by default** and are **trusted-use
> only**. When enabled, the injected MCP config carries the org-wide Composio API
> key into the agent-controlled task container — a task could exfiltrate it and
> reach every connected user's accounts. Acceptable for trusted/internal use; do
> not enable on an instance with untrusted users. Before external multi-tenant,
> move to a host-side MCP proxy so the org key never enters a task container. See
> [`docs/SECURITY.md`](docs/SECURITY.md).

## Configuration

All via environment variables — see `.env.example`.

## Notes / next steps

- **Auth**: users log in (email/password or Google); the session is a signed
  cookie. Set a stable `SUPERAGENT_SESSION_SECRET` and, when serving over https,
  `SUPERAGENT_COOKIE_SECURE=true`. There is no email verification / password
  reset yet (no SMTP) — add one before public signup.
- **Getting output files back**: the agent writes deliverables to
  `./artifacts/` in its `/workspace`, which persists per agent in a Docker
  volume. Browse and download them from the console's **Artifacts** view (backed
  by `GET /api/agents/{id}/files` and `.../file?path=...&dl=1`), or `docker cp`
  from the volume on the host.
- **Egress**: task containers use the `bridge` network (full egress). Tighten
  with a locked-down Docker network if you need to restrict where tasks reach.

## Security

See [`docs/SECURITY.md`](docs/SECURITY.md) for the threat model and hardening
notes. Two things to know up front:

- **Connectors are off by default and are trusted-use only.** Enabling Composio
  injects an org-wide key into agent-controlled task containers — do not enable it
  on an instance with untrusted users. See the connector caveat in `SECURITY.md`.
- **`SUPERAGENT_SESSION_SECRET` is durable.** It keys the at-rest encryption of
  data-source credentials and agent env; if it changes, that encrypted data
  becomes unrecoverable. Set a stable, backed-up value in production.

## License

Antonagents is source-available under the **Functional Source License**
([FSL-1.1-Apache-2.0](LICENSE.md)). In plain terms: you may **self-host, use, and
modify it freely** — for internal use, education, and research — but you may
**not** offer it to others as a competing commercial hosted service. Each release
automatically converts to **Apache 2.0** two years after it is published. See
[`LICENSE.md`](LICENSE.md) for the exact terms.

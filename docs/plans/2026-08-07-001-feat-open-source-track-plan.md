---
title: Open-source track — make Antonagents publishable (credible-minimal)
date: 2026-08-07
status: ready
owner: masaianshubham
artifact_readiness: implementation-ready
---

# Open-source track: make Antonagents a credible public repo

## Problem & goal

Antonagents is a working, self-hostable multi-tenant agent platform, currently in a
**private** repo (`masaianshubham/superagent`). We want to publish it as
open-source to serve the **self-host / data-residency** positioning: in this
category (Dify, n8n, Onyx, Executor are all OSS), being open is how you earn trust
and distribution, and it makes the "your data stays in your walls" claim credible
because the code is auditable.

**Goal of this track:** get the repo to a state where it can be flipped public and
be taken seriously by a self-host evaluator — without gold-plating. A later track
covers the paid managed-cloud (open-core) build; this track does **not**.

## Decisions (locked)

1. **License = source-available, non-compete.** Free to self-host, view, modify;
   nobody may offer it as a competing managed service. This protects a future
   managed-cloud offering. Concrete default: **FSL-1.1-Apache-2.0** (Functional
   Source License — non-compete, auto-converts to Apache-2.0 after 2 years).
   Acceptable alternatives if preferred at implementation time: n8n **Sustainable
   Use License** (no time conversion) or **BSL-1.1**. Pick one in Unit 1; don't
   re-litigate the *model*.
2. **Connectors (1b risk) = fenced, not fixed here.** The Composio org-key is
   injected into task containers (`app/connectors/composio_driver.py:278`) — an
   exfil path. Connectors are **already off by default**
   (`CONNECTORS_ENABLED = bool(COMPOSIO_API_KEY)`, `app/config.py:88`). We ship
   them off-by-default, document them as **trusted-use only**, and add a startup
   warning. The real host-side-proxy fix is a **separate track** (see Non-goals).
3. **Scope = credible-minimal.** License + containerized self-host (`docker
   compose up`) + accurate README/quickstart + CI running the existing tests +
   1b fenced + a `SECURITY.md`. Nothing beyond that.

## Scope

**In scope:** licensing, app containerization + compose, docs refresh, security
doc + connector fencing, CI, and a pre-publish checklist.

**Non-goals (explicit — deferred to later tracks):**
- Host-side MCP secret proxy / real 1b fix (its own security track).
- Managed-cloud build: Postgres, distributed queue/worker fleet, billing, KMS,
  execution-isolation hardening. (Separate "managed cloud" plan.)
- Polish-tier launch assets: `CONTRIBUTING.md`, issue/PR templates, screenshots/
  GIFs, demo seed data. (Do in the open after launch if wanted.)
- Changing the agent runtime (Claude SDK → Pi) — parked, unrelated.

## Pre-flight facts (verified 2026-08-07)

- Git history is **clean** — full-history secret audit across 60 commits found no
  keys, no `.env`/`.db` ever committed; only `.env.example` (placeholders).
- `.gitignore` covers `.env`, `.env.*`, `*.db`(+wal/shm), `.venv`, `__pycache__`.
- No hardcoded personal/host refs in shipped code (only a benign `localhost`
  default in `app/runner.py:71`).
- App is **not containerized** — runs via systemd `python -m app.main`; only the
  *task* image (`docker/Dockerfile.task`) exists.
- State layer is single-host: SQLite (`aiosqlite`) + in-process `AsyncIOScheduler`
  (`app/scheduler.py`). Fine for self-host; not a cloud concern here.

---

## Implementation units

### Unit 1 — License & governance
**Files:** `LICENSE.md` (new), `README.md` (Licensing section), `NOTICE` (new, optional).

- Add `LICENSE.md` with the chosen source-available non-compete text
  (default FSL-1.1-Apache-2.0; drop in the canonical text verbatim).
- Add a short **Licensing** section to `README.md`: one paragraph in plain English
  — "self-host freely, modify freely; you may not sell it as a competing hosted
  service," + when/whether it converts to OSS.
- If FSL: no per-file headers required; if BSL: add the change-date/grant params.

**Verify:** `LICENSE.md` present at repo root; README links to it; the plain-English
summary matches the license text (no contradiction).

### Unit 2 — Security doc + connector fencing (1b mitigation)
**Files:** `docs/SECURITY.md` (new), `app/config.py` or `app/main.py` (startup warning),
`README.md` (Security/Connectors note), `.env.example` (comment).

- Add `docs/SECURITY.md`: threat model in brief, the **connector caveat** (enabling
  Composio injects an org-wide key into agent-controlled containers → trusted-use
  only; do not enable on an instance with untrusted users), the at-rest encryption
  status (env + DSNs Fernet-encrypted; `SECRET_KEY` durability), and a
  responsible-disclosure contact.
- Confirm connectors stay **off by default** (they do) and add a one-line **startup
  log warning** when `CONNECTORS_ENABLED` is true (e.g. in `app/main.py` lifespan):
  "Connectors enabled — the provider key is injected into task containers; enable
  only for trusted users." Keep it a `logging.warning`, not a crash.
- Add a caution comment above `COMPOSIO_API_KEY=` in `.env.example`.

**Verify:** with `COMPOSIO_API_KEY` unset → NullConnector, no warning, app runs;
with it set → warning logged once at startup; `docs/SECURITY.md` renders and states
the caveat plainly.

### Unit 3 — App containerization + docker-compose self-host
**Files:** `docker/Dockerfile.app` (new), `docker-compose.yml` (new),
`.dockerignore` (new), `README.md` (compose quickstart).

- `docker/Dockerfile.app`: base python, install `requirements.txt`, install the
  **docker CLI** (the app shells out to `docker run` for each task run), copy `app/`
  + `web/`, `CMD python -m app.main`. Non-root where feasible (note: needs docker
  socket access).
- `docker-compose.yml`:
  - `app` service: build `Dockerfile.app`; `env_file: .env`; mount the host
    **Docker socket** (`/var/run/docker.sock`) so per-run task containers spawn as
    **siblings** on the host daemon; publish `8080`; volume for `data/` (SQLite DB).
  - Document that the **task image must be built first**
    (`docker build -t superagent-task:latest -f docker/Dockerfile.task agent/`) —
    or add a documented `make build-task` step; compose can't build the task image
    into the sibling daemon automatically.
  - Persist: `./data` (DB) and the per-agent named volumes
    (`superagent-agent-*-ws`) already live on the host daemon — unaffected by app
    container lifecycle.
- `.dockerignore`: exclude `.venv`, `data/`, `__pycache__`, `.git`, `web/fonts`
  optional.
- Add the **socket-mount security caveat** to `docs/SECURITY.md` (the app container
  is effectively host-root-equivalent — inherent to the per-run model; acceptable
  for single-tenant self-host).

**Verify (from a clean checkout):**
1. Build task image; `docker compose up` → `GET /app` returns 200.
2. Sign in, create an agent, run a one-shot task → a **sibling**
   `superagent-run-*` container appears (`docker ps`) and events stream to the UI.
3. Produce an artifact → it persists; restart `app` container → DB + artifact
   survive.

### Unit 4 — Docs refresh (README quickstart + config)
**Files:** `README.md`, `.env.example`.

- Fix stale references: README shows `POST /api/tasks`; the model is now
  **agents + runs**. Update the architecture blurb and any endpoint names.
- Add a **Quickstart** (compose path) alongside the existing host/systemd path:
  clone → build task image → `.env` (generate `SUPERAGENT_SESSION_SECRET`) →
  `docker compose up`.
- **`SECRET_KEY` durability warning** (prominent): it keys the Fernet encryption of
  DSNs and agent env; if it changes, encrypted data becomes unrecoverable — set a
  stable, backed-up value in prod.
- **Provider setup**: how to pick Anthropic vs GLM/Ollama (the router + `.env`).
- Keep it accurate over exhaustive.

**Verify:** a reader following only the README quickstart reaches a running instance;
no endpoint/name in README contradicts the code.

### Unit 5 — CI (run the existing tests)
**Files:** `.github/workflows/ci.yml` (new).

- GitHub Actions: on push/PR → set up Python, `pip install -r requirements.txt`,
  run `pytest -q`. (The suite is fast — 10 tests, ~0.6s.)
- Optional, low-cost: add `ruff check` if the repo already lints cleanly; skip if it
  would fail noisily (don't gold-plate).

**Verify:** workflow file is valid YAML; `pytest -q` passes locally (baseline: 10
passed); the job would go green on a clean runner (deps resolve from
`requirements.txt`).

### Unit 6 — Pre-publish checklist & flip to public
**Files:** none (process); optionally a `docs/RELEASE-CHECKLIST.md`.

- Re-run the full-history secret sweep one final time (expect clean).
- Confirm: `LICENSE.md` present, README quickstart works from a clean clone,
  `docker compose up` works, CI config committed, `docs/SECURITY.md` present,
  connectors off by default.
- **Manual (user action):** flip repo visibility to public on GitHub; optionally
  add topics/description; announce.

**Verify:** checklist all-green before the visibility flip. (This step is the only
near-irreversible action — do it last, deliberately.)

---

## Sequencing

Units are mostly independent; suggested order:
`Unit 1 (license)` → `Unit 2 (security/fencing)` → `Unit 3 (containerize)` →
`Unit 4 (docs)` → `Unit 5 (CI)` → `Unit 6 (checklist + flip)`.

Unit 3 is the largest (real work: Dockerfile.app + compose + socket-mount
verification). Units 1, 2, 5 are small. Unit 4 depends on Unit 3 (quickstart
documents the compose flow). Rough size: **~2–4 focused days.**

## Risks & mitigations

- **Docker-socket mount = host-root-equivalent for the app container.** Inherent to
  the sibling-container run model. Mitigation: document clearly in `SECURITY.md`;
  it's acceptable for single-tenant self-host (the intended OSS use).
- **`SECRET_KEY` rotation destroys encrypted data.** Mitigation: prominent README +
  `.env.example` warning; treat as durable/backed-up.
- **Task image not built before compose up.** Mitigation: document the build step
  explicitly (or a `make build-task`); compose can't build into the sibling daemon.
- **Non-permissive license deters some contributors.** Accepted tradeoff — the
  positioning (protect a managed cloud, data-residency) outweighs it; FSL's
  2-year OSS conversion softens it.
- **README staleness (`/api/tasks` → agents) confuses evaluators.** Fixed in Unit 4.
- **Publishing exposes the 1b attack path in public.** Mitigation: connectors
  off-by-default + `SECURITY.md` caveat + the real fix tracked separately; risk is
  live regardless of source visibility, so this doesn't create new exposure.

## Definition of done

Repo can be flipped public with: a source-available non-compete `LICENSE.md`; a
working `docker compose up` self-host verified from a clean clone; an accurate
README quickstart; `docs/SECURITY.md` documenting the connector + socket caveats;
CI running the test suite; connectors off by default with a startup warning when
enabled; and a final clean secret sweep.

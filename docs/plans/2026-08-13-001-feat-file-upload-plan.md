---
title: File upload in chat (workspace + anydoc for documents)
date: 2026-08-13
status: ready
owner: masaianshubham
artifact_readiness: implementation-ready
---

# File upload in chat

## Problem & goal

Users want to attach a file in a chat — a CSV to analyze, a PDF/Word doc to
summarize, a code file to review — and have the agent work on it. Today there's
no way to get a user's file into a chat; agents can only use DB data sources, the
web, and files they generate themselves.

**Goal:** let a user attach file(s) to a chat; the file lands in the agent's
workspace; the agent processes it with its tools and answers / produces artifacts.

## Design decision (settled) — the agentic model, not "send to the LLM"

The file is **not** stuffed into the model's context. It is written into the
agent's persistent `/workspace` volume (under `uploads/`), and the **agent
processes it with code** (pandas, anydoc, etc.), so it scales to large files and
can transform them, not just answer about them. The LLM sees the file **path** and
whatever the agent extracts. (True image→model vision is out of scope — see
Non-goals.)

## Grounding facts (verified 2026-08-13)

- The agent already reads/writes `/workspace`; file IO to the per-agent Docker
  volume goes **through the daemon** via a short-lived helper container
  (`runner.read_artifact`/`list_artifacts`, `docker run -v <vol>:/ws:ro …`) — this
  works on Linux/macOS/Windows. **Writing an upload is the mirror image** (`:rw`).
- The task image pre-bakes **`pandas`, `numpy`, `requests`, `ddgs`** — so
  **CSV/Excel/data files are already handled**. It has **no** PDF/Word parser.
- The sandbox agent is **non-root and cannot install packages at runtime**
  (`pip install pdfplumber` → PEP 668 "externally-managed" → fails). So documents
  **require a pre-baked parser** — the agent can't add one itself.
- **`firecrawl-anydoc`** (MIT, pure-Rust, no ML, no external service; Python pkg
  `firecrawl-anydoc` + CLI) converts Word/PPT/Excel/ODF/RTF/EPUB/CSV/**PDF** →
  clean Markdown, in-sandbox (residency-safe). No OCR (text PDFs only).
- `python-multipart` is installed but **not in `requirements.txt`** (add it).

## Scope

**In scope:** attach file(s) in chat → write into `/workspace/uploads/` → agent
processes them; anydoc bundled for documents; a system-prompt note so the agent
knows where uploads are and how to read them.

**Non-goals:**
- **Image→model vision** (passing an image as a multimodal content block). MVP
  treats images as files the agent can run image/OCR tools on; sending an image to
  the model is a separate, provider-dependent add (+1–2 days).
- **OCR for scanned/image-only PDFs** — anydoc handles text PDFs only. Add
  Tesseract (OSS, in-sandbox) later if a scanned-doc use case appears.
- Uploads for routines (this is chat-only for now).

---

## Implementation units

### Unit 1 — Volume-write helper + upload endpoint (backend)
**Files:** `app/runner.py`, `app/main.py`, `app/models.py` (none needed), `requirements.txt`.

- **`runner.write_upload(agent_id, filename, data: bytes) -> str`** — mirror of
  `read_artifact`: run a short-lived helper that mounts the agent's volume `:rw`
  and writes the bytes:
  ```
  docker run --rm -i --network none -v <vol>:/ws:rw --entrypoint sh <task-image>
    -c 'mkdir -p /ws/uploads && cat > "/ws/uploads/$1"' sa <safe_name>
  ```
  Pipe `data` to stdin. The helper runs as the image's `agent` user (uid 10001) —
  the same user that already owns/writes `/workspace`, so `mkdir`/write succeed.
  Returns `uploads/<safe_name>`. (Stream stdin for large files rather than holding
  everything in app memory.)
- **`POST /api/agents/{agent_id}/upload`** (`app/main.py`) — `UploadFile` multipart,
  `Depends(current_user)`, `_owned_agent_or_404`. Enforce a **size cap**
  (`SUPERAGENT_UPLOAD_MAX_MB`, default 25) and **sanitize the filename**
  (basename only; strip `..`/separators; allowlist chars; de-dupe collisions).
  Call `write_upload`; return `{name, path, size}`.
- Add `python-multipart` to `requirements.txt` (FastAPI needs it for `UploadFile`).

### Unit 2 — Bundle anydoc + agent note (task image + runner)
**Files:** `docker/Dockerfile.task`, `app/runner.py`. **Requires a task-image rebuild.**

- **`Dockerfile.task`**: add `firecrawl-anydoc` to the existing build-time
  `pip install --break-system-packages … pandas numpy requests ddgs` line. (Verify
  the exact package/CLI invocation at build; fall back to the npm CLI
  `@firecrawl/anydoc` — Node is already in the image — if the Python wheel is
  awkward.)
- **`_UPLOADS_NOTE`** in `runner.py`, injected into chat system prompts (same
  pattern as `_ARTIFACTS_NOTE`/`_SCHEDULING_NOTE`):
  > "Files the user uploads are in `./uploads/`. For **documents** (PDF, Word,
  > PowerPoint, Excel, ODF, RTF, EPUB), convert to clean Markdown first with the
  > `anydoc` tool, then work with the Markdown. For **CSV/tabular data** use
  > pandas. Read code/text files directly."
- On a turn that follows an upload, the agent must know a file arrived: the
  frontend appends `"[Attached: <names>]"` to the message (Unit 3), so the agent
  sees it and looks in `./uploads/`.

### Unit 3 — Composer attach UI (frontend)
**Files:** `web/console.html`.

- A **📎 attach button** in the `.composer` + a hidden `<input type=file multiple>`,
  and **drag-and-drop** onto the chat area.
- On select/drop: for each file, `POST …/upload` (multipart) with simple progress;
  render a **file chip** above the composer (name · size · remove ×). Track the
  attached names in a JS array (`PENDING_UPLOADS`).
- **Send flow** (`sendReply`): if `PENDING_UPLOADS` is non-empty, prepend
  `"[Attached: a.pdf, b.csv]\n"` to the prompt so the agent knows to read
  `./uploads/`; clear the chips after send.
- **Fresh chat:** if uploading before a chat exists (welcome state), create the
  chat first (reuse the existing create-on-first-message path), then upload to it.
- Reuse the existing **Artifacts viewer** styles for optional preview; image
  thumbnail is a nice-to-have, defer.

### Unit 4 — Limits, safety, tests
**Files:** `tests/…`, plus the guards in Units 1/3.

- Size cap enforced (reject > cap with a clear message).
- Filename sanitization + path-traversal guard (uploads confined to `/ws/uploads`).
- The sandbox is the security boundary (agent already runs arbitrary code there),
  so no content-type allowlist for MVP — just cap + sanitize.

---

## Verification (end-to-end, demo account)

Rebuild the task image (Unit 2 changed it), restart the app, then:

1. **CSV:** upload a small CSV → ask "summarize this" → agent uses pandas, answers.
2. **PDF (text):** upload a text PDF → agent runs `anydoc` → Markdown → summary.
3. **DOCX/XLSX:** upload → anydoc → Markdown → agent works with it.
4. **Multiple files** in one message → agent references both.
5. **Oversize** file → rejected with a clear error.
6. **Nasty filename** (`../../etc/x`, spaces, unicode) → sanitized, lands only in
   `uploads/`.
7. **Fresh welcome chat** upload → chat auto-created, file attached, turn runs.
8. **Cross-platform:** writes go through the daemon helper (no host path), so it
   works on macOS/Windows too.

## Sequencing & effort

`Unit 1 (backend)` → `Unit 2 (image + note)` → `Unit 3 (UI)` → `Unit 4 (safety/tests)`.

| Unit | Effort |
|---|---|
| 1 — write helper + endpoint + multipart dep | ~0.5 d |
| 2 — anydoc in task image + agent note (+ rebuild) | ~0.25 d |
| 3 — composer attach UI + chips + drag-drop + send wiring | ~1 d |
| 4 — limits + sanitize + tests | ~0.4 d |
| **Total** | **~1.5–2 days** |

## Risks & mitigations

- **anydoc packaging in the image** — verify the exact `firecrawl-anydoc`
  install + CLI at build; npm CLI (`@firecrawl/anydoc`) is the fallback (Node is
  present). Contained to `Dockerfile.task`.
- **No OCR** (scanned PDFs) — documented limitation; add Tesseract in-sandbox
  later if needed. Do **not** default to a hosted OCR service (breaks residency).
- **Task-image rebuild required** — same deploy gotcha as any `agent/` /
  `Dockerfile.task` change; the app-side code (Units 1/3) is host-side and needs
  only a normal restart.
- **Large files** — stream the upload to the helper's stdin rather than buffering
  the whole file in the app; enforce the size cap before writing.
- **Volume writability** — the helper writes as the `agent` user, which already
  owns `/workspace`; confirmed by the fact the agent writes artifacts there.

## Definition of done

A user can attach file(s) in a chat; they land in `/workspace/uploads/`; the agent
reads CSV/data with pandas and documents via anydoc, and answers / produces
artifacts — verified end-to-end for CSV + a text PDF, with size/filename guards,
on the live instance.

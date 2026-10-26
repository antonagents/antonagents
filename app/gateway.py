"""Action gateway — every connector call an agent makes passes through here.

Before: each run's container got the connector provider's MCP URL *and the
org-wide provider key*, so code in the container could call any tool, for any
user, unrecorded. Now the container gets only a per-run URL on this internal
listener (Docker bridge, never public). The gateway:

  * holds the provider key — it never enters a container;
  * classifies every action and applies the routine's guardrails
    (app/guardrails.py): allow → forward; approve → hold for a person;
    block → refuse;
  * records every action (the `actions` table) for audit and for the UI.

Held actions are run later by the platform itself, exactly once, exactly as the
agent requested them, after a person approves (execute_approved).
"""
import asyncio
import json
import logging
import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from . import config, db, guardrails

log = logging.getLogger("antonagents.gateway")

app = FastAPI(title="Antonagents action gateway", docs_url=None, redoc_url=None, openapi_url=None)
_RUNS: dict[str, dict] = {}          # token -> run context (runs end at restart, so memory is fine)
_client: Optional[httpx.AsyncClient] = None
_EXCERPT = 600
STATE = {"up": False, "url": None}


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15))
    return _client


# ---------------- run registration (called by the runner) ----------------

def register(run: dict, agent: dict, upstream: dict) -> Optional[dict]:
    """Swap a provider MCP server config for a per-run gateway one. Returns the
    config to give the container, or None if the gateway isn't running."""
    if not STATE["up"]:
        return None
    token = secrets.token_urlsafe(32)
    _RUNS[token] = {
        "run_id": run["id"], "outcome_id": run.get("outcome_id"), "agent_id": agent["id"],
        "org_id": agent["org_id"], "owner_id": agent.get("owner_id"),
        "upstream_url": upstream["url"], "upstream_headers": dict(upstream.get("headers") or {}),
        "policy": guardrails.policy_for(agent),
    }
    return {"type": "http", "url": f"{STATE['url']}/mcp/{token}"}


def unregister_run(run_id: int) -> None:
    for t in [t for t, c in _RUNS.items() if c["run_id"] == run_id]:
        _RUNS.pop(t, None)


def policy_for_run(run_id: int) -> Optional[dict]:
    return next((c["policy"] for c in _RUNS.values() if c["run_id"] == run_id), None)


# ---------------- MCP proxy ----------------

def _fwd_headers(request: Request, ctx: dict) -> dict:
    h = dict(ctx["upstream_headers"])
    for k in ("accept", "content-type", "mcp-session-id", "mcp-protocol-version", "last-event-id"):
        if k in request.headers:
            h[k] = request.headers[k]
    return h


async def _stream(request: Request, ctx: dict, method: str, body: Optional[bytes]):
    req = _http().build_request(method, ctx["upstream_url"], headers=_fwd_headers(request, ctx),
                                content=body)
    resp = await _http().send(req, stream=True)
    keep = {k: v for k, v in resp.headers.items()
            if k.lower() in ("content-type", "mcp-session-id", "cache-control")}
    return StreamingResponse(resp.aiter_raw(), status_code=resp.status_code, headers=keep,
                             background=BackgroundTask(resp.aclose))


def _parse_rpc(resp: httpx.Response) -> Optional[dict]:
    """A JSON-RPC message from a JSON or event-stream response."""
    text = resp.text
    if "event-stream" in resp.headers.get("content-type", ""):
        datas = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        for d in reversed(datas):
            try:
                m = json.loads(d)
                if isinstance(m, dict) and ("result" in m or "error" in m):
                    return m
            except ValueError:
                continue
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _result_text(m: Optional[dict]) -> str:
    if not m:
        return ""
    if "error" in m:
        return json.dumps(m["error"])[:_EXCERPT]
    parts = [c.get("text", "") for c in (m.get("result") or {}).get("content") or [] if isinstance(c, dict)]
    return " ".join(parts)[:_EXCERPT]


def _rpc_result(msg_id, text: str, is_error: bool = False) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                         "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def _explain(held: list[dict], blocked: list[dict]) -> str:
    lines = []
    for a in held:
        lines.append(f"⏸ HELD FOR APPROVAL — {a['tool_slug']} was NOT run. On this routine, actions that "
                     f"{guardrails.CATEGORIES[a['category']].split(' (')[0].lower()} need a person's "
                     f"approval. It is queued as action #{a['id']} and will run once, exactly as you "
                     f"requested it, if they approve. Do not retry it or find another way to do it; "
                     f"carry on with the rest of the job and mention it in your final message.")
    for a in blocked:
        lines.append(f"⛔ BLOCKED — {a['tool_slug']} is not allowed on this routine "
                     f"({a['category']} actions are blocked). Do not try another way to do it; say so "
                     f"in your final message.")
    return "\n".join(lines)


async def _record(ctx: dict, slug: str, args: dict, decision: str, category: str) -> dict:
    status = {"allow": "forwarded", "approve": "held", "block": "blocked"}[decision]
    aid = await db.add_action(org_id=ctx["org_id"], agent_id=ctx["agent_id"], run_id=ctx["run_id"],
                              outcome_id=ctx["outcome_id"], owner_id=ctx["owner_id"], tool_slug=slug,
                              category=category, decision=decision, status=status, args=args)
    return {"id": aid, "tool_slug": slug, "category": category, "decision": decision}


async def _tools_call(request: Request, ctx: dict, msg: dict):
    params = msg.get("params") or {}
    name = str(params.get("name") or "")
    args = params.get("arguments") or {}
    if name in guardrails.DISCOVERY_TOOLS:          # finding tools isn't doing anything
        return await _stream(request, ctx, "POST", json.dumps(msg).encode())
    if name in guardrails.ADMIN_TOOLS and not args.get("reinitiate_all"):
        # checking which apps are connected is how the router starts every task;
        # only re-initiating connections changes anything
        return await _stream(request, ctx, "POST", json.dumps(msg).encode())

    if name == guardrails.EXECUTE_TOOL:
        items = [t for t in (args.get("tools") or []) if isinstance(t, dict)]
    else:   # code / admin meta-tools, or a plain tool on a non-router server
        items = [{"tool_slug": name, "arguments": args, "_whole": True}]

    allowed, held, blocked = [], [], []
    for it in items:
        slug = str(it.get("tool_slug") or "")
        decision, cat = guardrails.decide(slug, ctx["policy"])
        if cat in ("code", "admin") and decision == "approve":
            decision = "block"       # opaque: there's nothing exact to approve and replay later
        rec = await _record(ctx, slug, it.get("arguments") or {}, decision, cat)
        (allowed if decision == "allow" else held if decision == "approve" else blocked).append({**rec, "item": it})

    note = _explain(held, blocked)
    if not allowed:
        return _rpc_result(msg.get("id"), note, is_error=bool(blocked and not held))

    if held or blocked:      # run only the allowed part of a batch
        if name == guardrails.EXECUTE_TOOL:
            args = {**args, "tools": [{k: v for k, v in a["item"].items() if not k.startswith("_")}
                                      for a in allowed]}
        msg = {**msg, "params": {**params, "arguments": args}}
    resp = await _http().post(ctx["upstream_url"], headers=_fwd_headers(request, ctx),
                              content=json.dumps(msg).encode())
    rpc = _parse_rpc(resp)
    failed = resp.status_code >= 400 or not rpc or "error" in rpc or (rpc.get("result") or {}).get("isError")
    excerpt = _result_text(rpc) or f"HTTP {resp.status_code}"
    for a in allowed:
        await db.set_action(a["id"], status="failed" if failed else "executed",
                            result=None if failed else excerpt, error=excerpt if failed else None)
    if not note:            # nothing held or blocked: hand the upstream answer back untouched
        keep = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "mcp-session-id")}
        return Response(resp.content, status_code=resp.status_code, headers=keep)
    if rpc and "result" in rpc:
        rpc["result"].setdefault("content", []).append({"type": "text", "text": note})
        return JSONResponse(rpc)
    return _rpc_result(msg.get("id"), f"{excerpt}\n{note}", is_error=True)


@app.api_route("/mcp/{token}", methods=["POST", "GET", "DELETE"])
async def mcp(token: str, request: Request):
    ctx = _RUNS.get(token)
    if not ctx:
        return JSONResponse({"error": "unknown or finished run"}, status_code=404)
    if request.method != "POST":
        return await _stream(request, ctx, request.method, None)
    body = await request.body()
    try:
        msg = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if isinstance(msg, dict) and msg.get("method") == "tools/call":
        try:
            return await _tools_call(request, ctx, msg)
        except Exception as e:  # noqa: BLE001 - a gateway fault must never run the action unchecked
            log.exception("gateway tools/call failed")
            await db.release()
            return _rpc_result(msg.get("id"), f"The action gateway failed ({type(e).__name__}); "
                                              "the action was not run.", is_error=True)
    if isinstance(msg, list) and any(isinstance(m, dict) and m.get("method") == "tools/call" for m in msg):
        return JSONResponse({"error": "batched tool calls aren't supported"}, status_code=400)
    return await _stream(request, ctx, "POST", body)


# ---------------- approvals ----------------

async def execute_approved(action_id: int, approver: str) -> dict:
    """Run a held action once, as the routine's owner, after a person approved it."""
    if not await db.set_action(action_id, status="approved", decided_by=approver, only_if="held"):
        a = await db.get_action(action_id)
        raise ValueError(f"action #{action_id} is {a['status'] if a else 'missing'}, not waiting")
    a = await db.get_action(action_id)
    from .deliver import sdk_execute
    try:
        ok, data, err = await sdk_execute(a["tool_slug"], a["args"], a["owner_id"])
        if ok:
            await db.set_action(action_id, status="executed", result=json.dumps(data, default=str)[:_EXCERPT])
        else:
            await db.set_action(action_id, status="failed", error=str(err)[:_EXCERPT])
    except Exception as e:  # noqa: BLE001
        await db.set_action(action_id, status="failed", error=f"{type(e).__name__}: {e}"[:_EXCERPT])
    return await db.get_action(action_id)


async def reject(action_id: int, approver: str, note: str = "") -> dict:
    if not await db.set_action(action_id, status="rejected", decided_by=approver,
                               error=note[:300] or None, only_if="held"):
        a = await db.get_action(action_id)
        raise ValueError(f"action #{action_id} is {a['status'] if a else 'missing'}, not waiting")
    return await db.get_action(action_id)


# ---------------- the internal listener ----------------

async def _bridge_ip() -> Optional[str]:
    try:
        p = await asyncio.create_subprocess_exec(
            "docker", "network", "inspect", config.TASK_NETWORK,
            "--format", "{{range .IPAM.Config}}{{.Gateway}}{{end}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=15)
        ip = out.decode().strip()
        return ip or None
    except Exception:  # noqa: BLE001
        return None


async def serve() -> None:
    """Run the gateway on the task network's gateway address (reachable from task
    containers, not from the internet). If it can't start, runs get no connectors
    rather than the provider key."""
    import uvicorn
    host = config.GATEWAY_HOST or await _bridge_ip()
    if not host:
        log.error("action gateway: couldn't find the task network's gateway address; "
                  "connectors are disabled for runs")
        return
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=config.GATEWAY_PORT,
                                           log_level="warning", lifespan="off"))
    server.install_signal_handlers = lambda: None      # the main app owns signals
    task = asyncio.create_task(server.serve())
    for _ in range(100):                               # mark it up only once it's bound
        if server.started or task.done():
            break
        await asyncio.sleep(0.1)
    if not server.started:
        log.error("action gateway failed to start on %s:%s; connectors are disabled for runs",
                  host, config.GATEWAY_PORT)
        return
    STATE.update(up=True, url=f"http://{host}:{config.GATEWAY_PORT}")
    log.info("action gateway listening on %s", STATE["url"])
    try:
        await task
    finally:
        STATE["up"] = False

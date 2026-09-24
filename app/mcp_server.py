"""Each agent as an MCP server (Streamable HTTP transport, stateless).

Endpoint: POST /v1/agents/{public_id}/mcp, authenticated with the agent's
`Authorization: Bearer sk_ag_…` key — so any MCP client (Claude Code, Claude
Desktop, Cursor, another agent) can mount one of our agents as a tool.

Tools:
    ask(message)   run the agent on a message (continuing its conversation) and
                   return its reply
    run()          run the agent's standing instruction (only if it has one)
    get_run(run_id) fetch a run's status/result — the fallback when a long run
                   outlives the wait

Why hand-rolled rather than the `mcp` SDK: the transport we need is small
(initialize / ping / tools/list / tools/call over one POST), and doing it here
keeps auth, per-key rate limiting and agent scoping on the exact same path as
the REST /v1 routes instead of bridging them into a separate ASGI app. It is
stateless (no Mcp-Session-Id), so it works unchanged behind any proxy.

Long runs: agent runs take minutes, longer than many clients' request timeout.
When the client accepts `text/event-stream`, a tools/call is answered as an SSE
stream that sends `notifications/progress` while the run works (clients that
reset their timeout on progress stay connected), then the result. If the run
still isn't done after MCP_WAIT_SECONDS, the tool returns the run id and the
caller finishes with get_run.
"""
import asyncio
import json
from typing import Any, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, db, serving

# newest first; we echo the client's version when we support it
PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

# JSON-RPC error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


def _ok(req_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _text(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tools_for(agent: dict) -> list[dict]:
    name = agent.get("name") or "agent"
    ask_desc = (
        f"Send a message to the \"{name}\" agent and get its reply. The agent works in "
        "its own isolated sandbox with its own tools, connectors and data sources, so "
        "a request can take several minutes. Follow-up messages continue the same "
        "conversation."
    )
    tools = [{
        "name": "ask",
        "title": f"Ask {name}",
        "description": ask_desc,
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string",
                                       "description": "What you want the agent to do or answer."}},
            "required": ["message"],
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": True},
    }]
    if (agent.get("prompt") or "").strip():
        tools.append({
            "name": "run",
            "title": f"Run {name}",
            "description": (f"Run the \"{name}\" agent's standing instruction: "
                            f"{_clip(agent['prompt'], 400)}"),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": False, "openWorldHint": True},
        })
    tools.append({
        "name": "get_run",
        "title": "Get run result",
        "description": ("Get the status and result of a run this agent started earlier "
                        "(use it when ask/run said the agent is still working)."),
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "integer", "description": "The run id."}},
            "required": ["run_id"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    })
    return tools


def _run_to_tool_result(run: Optional[dict]) -> dict:
    """Render a (possibly unfinished) run as an MCP tool result."""
    if not run:
        return _text("run not found", is_error=True)
    status = run["status"]
    if status == "succeeded":
        return _text(run.get("result") or "(the agent finished without a text reply)")
    if status in serving.TERMINAL:
        return _text(f"The agent run {run['id']} {status}: {run.get('error') or 'no details'}",
                     is_error=True)
    return _text(f"The agent is still working (run {run['id']}, status: {status}). "
                 f"Call get_run with run_id={run['id']} in a minute to get the result.")


def _initialize(agent: dict, params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
    name = agent.get("name") or "agent"
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": f"antonagents-{agent.get('public_id')}", "title": name,
                       "version": config.APP_VERSION},
        "instructions": (f"This server is the \"{name}\" agent on Antonagents. Use the "
                         "`ask` tool to give it a task; it replies when the run finishes."),
    }


def _progress_note(token: Any, n: int, ev: Optional[dict]) -> dict:
    """A notifications/progress frame describing what the agent is doing."""
    if ev is None:
        msg = "working…"
    elif ev.get("type") == "tool_use":
        msg = f"using {ev.get('name') or 'a tool'}"
    elif ev.get("type") == "assistant_text":
        msg = _clip(ev.get("text") or "", 200)
    elif ev.get("type") == "status":
        msg = ev.get("status") or "working…"
    else:
        msg = ev.get("type") or "working…"
    return {"jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"progressToken": token, "progress": n, "message": msg}}


def _sse(msg: dict) -> str:
    return f"event: message\ndata: {json.dumps(msg)}\n\n"


async def _start_tool_run(agent: dict, name: str, args: dict) -> "int | dict":
    """Start the run a tool call asks for. Returns the run id, or a JSON-RPC-level
    tool result dict when the arguments are unusable."""
    if name == "ask":
        message = args.get("message")
        if not isinstance(message, str) or not message.strip():
            return _text("`message` is required", is_error=True)
        if len(message) > 100000:
            return _text("`message` is too long (max 100000 characters)", is_error=True)
        return await serving.start_chat(agent, message)
    # name == "run"
    return await serving.start_default(agent)


async def _call_tool_sync(agent: dict, name: str, args: dict) -> dict:
    """tools/call answered as a single JSON response (waits for the run)."""
    if name == "get_run":
        return await _get_run_result(agent, args)
    started = await _start_tool_run(agent, name, args)
    if isinstance(started, dict):
        return started
    run = await serving.wait_for_run(started, config.MCP_WAIT_SECONDS)
    return _run_to_tool_result(run)


async def _get_run_result(agent: dict, args: dict) -> dict:
    run_id = args.get("run_id")
    if isinstance(run_id, str) and run_id.isdigit():
        run_id = int(run_id)
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        return _text("`run_id` must be an integer", is_error=True)
    run = await db.get_run(run_id)
    if not run or run.get("agent_id") != agent["id"]:
        return _text("run not found", is_error=True)
    return _run_to_tool_result(run)


def _tool_names(agent: dict) -> set[str]:
    return {t["name"] for t in tools_for(agent)}


async def handle(agent: dict, request: Request) -> Response:
    """Handle one POST to the agent's MCP endpoint."""
    try:
        payload = json.loads(await request.body() or b"null")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(_err(None, PARSE_ERROR, "parse error"), status_code=400)

    # one message, or (2025-03-26) a batch of them
    batch = isinstance(payload, list)
    messages = payload if batch else [payload]
    if not messages or not all(isinstance(m, dict) for m in messages):
        return JSONResponse(_err(None, INVALID_REQUEST, "invalid request"), status_code=400)

    requests = [m for m in messages if "method" in m and "id" in m]
    if not requests:
        # only notifications (e.g. notifications/initialized) or client responses
        return Response(status_code=202)

    wants_sse = "text/event-stream" in (request.headers.get("accept") or "")
    # a single long-running tool call streams progress when the client allows it
    if not batch and wants_sse and requests[0].get("method") == "tools/call":
        params = requests[0].get("params") or {}
        if (isinstance(params, dict) and params.get("name") in ("ask", "run")
                and params.get("name") in _tool_names(agent)):
            return await _stream_tool_call(agent, requests[0])

    replies = [await _dispatch(agent, m) for m in requests]
    return JSONResponse(replies if batch else replies[0])


async def _dispatch(agent: dict, msg: dict) -> dict:
    req_id, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _err(req_id, INVALID_PARAMS, "params must be an object")
    if method == "initialize":
        return _ok(req_id, _initialize(agent, params))
    if method == "ping":
        return _ok(req_id, {})
    if method == "tools/list":
        return _ok(req_id, {"tools": tools_for(agent)})
    if method == "tools/call":
        name = params.get("name")
        if name not in _tool_names(agent):
            return _err(req_id, INVALID_PARAMS, f"unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(req_id, INVALID_PARAMS, "arguments must be an object")
        return _ok(req_id, await _call_tool_sync(agent, name, args))
    return _err(req_id, METHOD_NOT_FOUND, f"method not found: {method}")


async def _stream_tool_call(agent: dict, msg: dict) -> Response:
    """Answer an ask/run tools/call as SSE: progress notifications, then the result."""
    req_id = msg.get("id")
    params = msg.get("params") or {}
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        return JSONResponse(_err(req_id, INVALID_PARAMS, "arguments must be an object"))
    token = (params.get("_meta") or {}).get("progressToken")

    # start the run before the response begins, so it exists even if the client
    # drops the stream (it keeps running; get_run fetches it later)
    started = await _start_tool_run(agent, params["name"], args)
    if isinstance(started, dict):
        return JSONResponse(_ok(req_id, started))

    frames: asyncio.Queue = asyncio.Queue()
    n = 0

    async def on_event(ev: Optional[dict]) -> None:
        nonlocal n
        # surface meaningful activity (and silence ticks); skip the noisy rest
        if ev is not None and ev.get("type") not in ("tool_use", "assistant_text", "status"):
            return
        n += 1
        frames.put_nowait(_sse(_progress_note(token, n, ev)) if token is not None
                          else ": keepalive\n\n")

    async def worker() -> None:
        try:
            run = await serving.wait_for_run(started, config.MCP_WAIT_SECONDS,
                                             on_event=on_event, tick=10.0)
            frames.put_nowait(_sse(_ok(req_id, _run_to_tool_result(run))))
        except Exception as e:  # noqa: BLE001 - always end the stream with a reply
            frames.put_nowait(_sse(_ok(req_id, _text(
                f"error waiting for run {started}: {type(e).__name__}; "
                f"call get_run with run_id={started}", is_error=True))))
        finally:
            frames.put_nowait(None)

    async def gen():
        task = asyncio.create_task(worker())
        try:
            # first frame right away so proxies/clients see the stream is alive
            yield ": run started\n\n"
            while (frame := await frames.get()) is not None:
                yield frame
        finally:
            if not task.done():   # client went away; the run itself keeps going
                task.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

"""Tests for serving agents over REST (/v1 wait + stream) and MCP.

Drives the real FastAPI app through httpx's ASGI transport against a throwaway
SQLite file. The Docker runner is swapped for a fake that finishes each run with
an echo of its prompt, so no containers are needed.
"""
import asyncio
import json
import os
import tempfile

import httpx

from app import config, db, main, ratelimit, runner


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    config.DB_PATH = path


def _fake_runner(monkeypatch, *, finish=True, delay=0.05):
    """Replace start_run: emit one tool_use event, then (optionally) succeed with
    `echo: <prompt>` — publishing the same events/sentinel the real runner does."""
    async def fake_start_run(run_id, agent):
        async def go():
            await asyncio.sleep(delay)
            await db.mark_run_started(run_id)
            ev = {"type": "tool_use", "name": "Bash", "input": {}}
            await db.add_event(run_id, 1, "tool_use", ev)
            runner._publish(run_id, {"seq": 1, **ev})
            if not finish:
                return
            run = await db.get_run(run_id)
            await db.finish_run(run_id, status="succeeded", exit_code=0,
                                result=f"echo: {run['prompt']}")
            runner._publish(run_id, {"type": "status", "status": "succeeded"})
            runner._publish(run_id, None)
        asyncio.create_task(go())
    monkeypatch.setattr(runner, "start_run", fake_start_run)
    monkeypatch.setattr(ratelimit, "v1_limiter", ratelimit.TokenBucketLimiter(0))


async def _setup():
    """Two agents in one org, each with its own key."""
    await db.init()
    org = await db.create_org("Org")
    uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
    a1 = await db.create_agent({"name": "Reporter", "prompt": "write the daily report"},
                               org_id=org, owner_id=uid)
    a2 = await db.create_agent({"name": "Other", "prompt": ""}, org_id=org, owner_id=uid)
    k1, _ = await db.create_api_key(a1, org, name="t", created_by=uid)
    k2, _ = await db.create_api_key(a2, org, name="t", created_by=uid)
    p1 = (await db.get_agent(a1))["public_id"]
    p2 = (await db.get_agent(a2))["public_id"]
    return {"k1": k1, "k2": k2, "p1": p1, "p2": p2}


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                             base_url="http://test")


def _run(scenario):
    async def wrapped():
        try:
            await scenario()
        finally:
            await db.close()
    asyncio.run(wrapped())


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# ---------------- REST ----------------

def test_v1_chat_wait_returns_result(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch)

    async def scenario():
        s = await _setup()
        async with _client() as c:
            # default stays async: just the run id (backward compatible)
            r = await c.post(f"/v1/agents/{s['p1']}/chat", json={"message": "hi"},
                             headers=_auth(s["k1"]))
            assert r.status_code == 200 and set(r.json()) == {"run_id", "agent_id"}

            # ?wait=N blocks for the finished answer
            r = await c.post(f"/v1/agents/{s['p1']}/chat?wait=10", json={"message": "hello"},
                             headers=_auth(s["k1"]))
            body = r.json()
            assert body["done"] is True and body["status"] == "succeeded"
            assert body["result"] == "echo: hello" and body["agent_id"] == s["p1"]

            r = await c.post(f"/v1/agents/{s['p1']}/run?wait=10", headers=_auth(s["k1"]))
            assert r.json()["result"] == "echo: write the daily report"

    _run(scenario)


def test_v1_wait_times_out_with_running_state(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch, finish=False)

    async def scenario():
        s = await _setup()
        async with _client() as c:
            r = await c.post(f"/v1/agents/{s['p1']}/chat?wait=1", json={"message": "slow"},
                             headers=_auth(s["k1"]))
            body = r.json()
            assert body["done"] is False and body["status"] in ("queued", "running")
            assert body["result"] is None

    _run(scenario)


def test_v1_agent_info_stream_and_scoping(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch)

    async def scenario():
        s = await _setup()
        async with _client() as c:
            r = await c.get(f"/v1/agents/{s['p1']}", headers=_auth(s["k1"]))
            info = r.json()
            assert info["name"] == "Reporter" and info["has_default_instruction"] is True
            assert info["mcp_url"].endswith(f"/v1/agents/{s['p1']}/mcp")

            run_id = (await c.post(f"/v1/agents/{s['p1']}/chat", json={"message": "x"},
                                   headers=_auth(s["k1"]))).json()["run_id"]
            async with c.stream("GET", f"/v1/runs/{run_id}/stream?public_id={s['p1']}",
                                headers=_auth(s["k1"])) as resp:
                assert resp.headers["content-type"].startswith("text/event-stream")
                frames = [json.loads(line[6:]) async for line in resp.aiter_lines()
                          if line.startswith("data: ")]
            assert [f["type"] for f in frames] == ["tool_use", "status"]
            assert frames[-1]["status"] == "succeeded"

            # agent 2's key can't reach agent 1, nor stream agent 1's run
            r = await c.get(f"/v1/agents/{s['p1']}", headers=_auth(s["k2"]))
            assert r.status_code == 403
            r = await c.get(f"/v1/runs/{run_id}/stream?public_id={s['p2']}",
                            headers=_auth(s["k2"]))
            assert r.status_code == 404
            r = await c.get(f"/v1/agents/{s['p1']}")
            assert r.status_code == 401

    _run(scenario)


# ---------------- MCP ----------------

def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "method": method, "id": id_}
    if params is not None:
        msg["params"] = params
    return msg


def test_mcp_handshake_and_tools(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch)

    async def scenario():
        s = await _setup()
        url = f"/v1/agents/{s['p1']}/mcp"
        h = {**_auth(s["k1"]), "Accept": "application/json, text/event-stream"}
        async with _client() as c:
            r = await c.post(url, json=_rpc("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"}}), headers=h)
            init = r.json()["result"]
            assert init["protocolVersion"] == "2025-06-18"
            assert "tools" in init["capabilities"]
            assert init["serverInfo"]["title"] == "Reporter"

            # notifications get 202 and no body
            r = await c.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                             headers=h)
            assert r.status_code == 202 and r.content == b""

            tools = (await c.post(url, json=_rpc("tools/list"), headers=h)).json()["result"]["tools"]
            assert [t["name"] for t in tools] == ["ask", "run", "get_run"]
            assert "write the daily report" in tools[1]["description"]

            # an agent with no standing instruction has no `run` tool
            h2 = {**_auth(s["k2"]), "Accept": "application/json"}
            tools2 = (await c.post(f"/v1/agents/{s['p2']}/mcp", json=_rpc("tools/list"),
                                   headers=h2)).json()["result"]["tools"]
            assert [t["name"] for t in tools2] == ["ask", "get_run"]

            r = await c.post(url, json=_rpc("resources/list"), headers=h)
            assert r.json()["error"]["code"] == -32601
            r = await c.post(url, json=_rpc("tools/call", {"name": "nope", "arguments": {}}),
                             headers=h)
            assert r.json()["error"]["code"] == -32602
            r = await c.post(url, content=b"{not json", headers=h)
            assert r.status_code == 400 and r.json()["error"]["code"] == -32700

            assert (await c.get(url, headers=h)).status_code == 405
            assert (await c.post(url, json=_rpc("ping"))).status_code == 401

    _run(scenario)


def test_mcp_ask_json_and_get_run(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch)

    async def scenario():
        s = await _setup()
        url = f"/v1/agents/{s['p1']}/mcp"
        h = {**_auth(s["k1"]), "Accept": "application/json"}
        async with _client() as c:
            r = await c.post(url, json=_rpc("tools/call", {
                "name": "ask", "arguments": {"message": "what's up"}}), headers=h)
            res = r.json()["result"]
            assert res["isError"] is False
            assert res["content"][0]["text"] == "echo: what's up"

            bad = await c.post(url, json=_rpc("tools/call", {
                "name": "ask", "arguments": {"message": "  "}}), headers=h)
            assert bad.json()["result"]["isError"] is True

            # get_run: own run resolves; another agent's run is invisible
            own = await db.create_run((await db.get_agent_by_public_id(s["p1"]))["id"],
                                      prompt="p")
            await db.finish_run(own, status="succeeded", result="done!")
            r = await c.post(url, json=_rpc("tools/call", {
                "name": "get_run", "arguments": {"run_id": own}}), headers=h)
            assert r.json()["result"]["content"][0]["text"] == "done!"

            other = await db.create_run((await db.get_agent_by_public_id(s["p2"]))["id"],
                                        prompt="secret")
            await db.finish_run(other, status="succeeded", result="secret result")
            r = await c.post(url, json=_rpc("tools/call", {
                "name": "get_run", "arguments": {"run_id": other}}), headers=h)
            res = r.json()["result"]
            assert res["isError"] is True and "secret" not in res["content"][0]["text"]

    _run(scenario)


def test_mcp_ask_streams_progress_then_result(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch)

    async def scenario():
        s = await _setup()
        url = f"/v1/agents/{s['p1']}/mcp"
        h = {**_auth(s["k1"]), "Accept": "application/json, text/event-stream"}
        msg = _rpc("tools/call", {"name": "ask", "arguments": {"message": "go"},
                                  "_meta": {"progressToken": "tok"}}, id_=7)
        async with _client() as c:
            async with c.stream("POST", url, json=msg, headers=h) as resp:
                assert resp.headers["content-type"].startswith("text/event-stream")
                msgs = [json.loads(line[6:]) async for line in resp.aiter_lines()
                        if line.startswith("data: ")]
        progress = [m for m in msgs if m.get("method") == "notifications/progress"]
        assert progress and progress[0]["params"]["progressToken"] == "tok"
        assert progress[0]["params"]["message"] == "using Bash"
        final = msgs[-1]
        assert final["id"] == 7 and final["result"]["content"][0]["text"] == "echo: go"

    _run(scenario)


def test_mcp_ask_returns_run_id_when_wait_exceeded(monkeypatch):
    _fresh_db()
    _fake_runner(monkeypatch, finish=False)
    monkeypatch.setattr(config, "MCP_WAIT_SECONDS", 1)

    async def scenario():
        s = await _setup()
        url = f"/v1/agents/{s['p1']}/mcp"
        async with _client() as c:
            r = await c.post(url, json=_rpc("tools/call", {
                "name": "ask", "arguments": {"message": "slow"}}),
                headers={**_auth(s["k1"]), "Accept": "application/json"})
        res = r.json()["result"]
        assert res["isError"] is False
        assert "still working" in res["content"][0]["text"]
        assert "get_run" in res["content"][0]["text"]

    _run(scenario)

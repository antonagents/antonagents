"""Tests for the Agent Builder foundation (U1 org isolation, U2 agent model).

Run: `. .venv/bin/activate && pytest`

Uses a throwaway SQLite file per test (via config.DB_PATH) and drives the async
db layer with asyncio.run, so no pytest-asyncio dependency is needed.
"""
import asyncio
import os
import tempfile

from app import config, db


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    config.DB_PATH = path
    return path


def _run(coro):
    return asyncio.run(coro())


def test_org_isolation():
    """AE1: an agent/run in org A is invisible to org B."""
    _fresh_db()

    async def scenario():
        await db.init()
        try:
            org_a = await db.create_org("A")
            org_b = await db.create_org("B")
            ua = await db.create_user("a@x.com", org_id=org_a, password_hash="h")
            await db.create_user("b@x.com", org_id=org_b, password_hash="h")

            agent_a = await db.create_agent(
                {"name": "A's agent", "prompt": "do a thing"}, org_id=org_a, owner_id=ua)
            run_a = await db.create_run(agent_a, prompt="do a thing")

            # A sees its own agent + run
            assert (await db.get_owned_agent(agent_a, org_a)) is not None
            assert (await db.get_owned_run(run_a, org_a)) is not None
            assert len(await db.list_agents(org_a)) == 1

            # B sees nothing of A's
            assert (await db.get_owned_agent(agent_a, org_b)) is None
            assert (await db.get_owned_run(run_a, org_b)) is None
            assert await db.list_agents(org_b) == []
        finally:
            await db.close()

    _run(scenario)


def test_agent_run_linkage():
    """U2: runs belong to an agent; last_run and list_runs resolve by agent."""
    _fresh_db()

    async def scenario():
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            agent = await db.create_agent(
                {"name": "agent", "prompt": "p"}, org_id=org, owner_id=uid)

            r1 = await db.create_run(agent, prompt="first")
            r2 = await db.create_run(agent, prompt="second (chat)", resume=True)

            runs = await db.list_runs(agent)
            assert {r["id"] for r in runs} == {r1, r2}
            last = await db.last_run(agent)
            assert last["id"] == r2
            # a chat/follow-up run is flagged to resume the session
            r2_row = await db.get_run(r2)
            assert r2_row["resume"] == 1 and r2_row["prompt"] == "second (chat)"
        finally:
            await db.close()

    _run(scenario)


def test_agent_connector_and_skill_binding():
    """U3: connectors and skills bind to an agent and read back; rebinding replaces."""
    _fresh_db()

    async def scenario():
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            agent = await db.create_agent({"name": "a", "prompt": "p"}, org_id=org, owner_id=uid)

            await db.set_agent_connectors(agent, ["slack", "linear"])
            assert set(await db.get_agent_connectors(agent)) == {"slack", "linear"}
            # rebinding replaces, not appends
            await db.set_agent_connectors(agent, ["gmail"])
            assert await db.get_agent_connectors(agent) == ["gmail"]

            s1 = await db.create_skill(org, "triage", instructions="group by urgency")
            s2 = await db.create_skill(org, "digest", instructions="summarize weekly")
            await db.set_agent_skills(agent, [s1, s2])
            names = {s["name"] for s in await db.get_agent_skills(agent)}
            assert names == {"triage", "digest"}
            # skills are org-scoped (every org also carries the built-in
            # Artifact Design skill, seeded on org creation)
            assert {"triage", "digest"} <= {s["name"] for s in await db.list_skills(org)}
        finally:
            await db.close()

    _run(scenario)


def test_db_connection_encryption_and_binding():
    """U4: DSNs encrypt at rest, list omits them, and connections bind to agents."""
    _fresh_db()

    async def scenario():
        from app.connectors import db_driver
        await db.init()
        try:
            dsn = "postgresql://user:s3cret@host:5432/orders"
            enc = db_driver.encrypt(dsn)
            assert enc != dsn and "s3cret" not in enc          # ciphertext hides the password
            assert db_driver.decrypt(enc) == dsn                # round-trips

            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            cid = await db.create_db_connection(
                org, uid, name="orders", engine="postgres", dsn_encrypted=enc, mode="read_only")

            listed = await db.list_db_connections(org)
            assert listed[0]["name"] == "orders"
            assert "dsn_encrypted" not in listed[0] and "dsn" not in listed[0]  # never listed
            full = await db.get_db_connection(cid, org)
            assert "s3cret" not in full["dsn_encrypted"]        # stored ciphertext only

            agent = await db.create_agent({"name": "a", "prompt": "p"}, org_id=org, owner_id=uid)
            await db.set_agent_db_connections(agent, [cid])
            assert (await db.get_agent_db_connections(agent))[0]["name"] == "orders"

            assert "default_transaction_read_only" in db_driver.effective_dsn("postgres", dsn, "read_only")
            assert db_driver.env_var("orders") == "SUPERAGENT_DB_ORDERS"
        finally:
            await db.close()

    _run(scenario)


def test_schedule_update_and_validation():
    """U6: schedule persists on the agent; invalid cron/interval exprs are rejected."""
    _fresh_db()

    async def scenario():
        from app import scheduler
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            agent = await db.create_agent({"name": "a", "prompt": "p"}, org_id=org, owner_id=uid)

            await db.set_agent_schedule(agent, "interval", "3600")
            a = await db.get_owned_agent(agent, org)
            assert a["schedule_kind"] == "interval" and a["schedule_expr"] == "3600"

            await db.set_agent_schedule(agent, "cron", "0 9 * * *")
            a = await db.get_owned_agent(agent, org)
            assert a["schedule_kind"] == "cron" and a["schedule_expr"] == "0 9 * * *"

            # validation: valid passes, invalid raises (independent of a live scheduler)
            scheduler.validate_schedule("cron", "0 9 * * *")
            scheduler.validate_schedule("interval", "3600")
            scheduler.validate_schedule("once", None)
            for bad in [("cron", "not a cron"), ("interval", "abc")]:
                try:
                    scheduler.validate_schedule(*bad)
                    assert False, f"expected {bad} to raise"
                except Exception:
                    pass
        finally:
            await db.close()

    _run(scenario)


def test_impact_math():
    """U8: only succeeded runs count; minutes = ok_runs * baseline; org rolls up agents."""
    _fresh_db()

    async def scenario():
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            a1 = await db.create_agent(
                {"name": "a1", "prompt": "p", "baseline_minutes": 30}, org_id=org, owner_id=uid)
            a2 = await db.create_agent(
                {"name": "a2", "prompt": "p", "baseline_minutes": 15}, org_id=org, owner_id=uid)

            # a1: two succeed, one fails → 2 * 30 = 60 min saved; spend sums only set costs
            for prompt, status, cost in [("r", "succeeded", 0.10),
                                         ("r", "succeeded", 0.20),
                                         ("r", "failed", 0.05)]:
                rid = await db.create_run(a1, prompt=prompt)
                await db.finish_run(rid, status=status, cost_usd=cost)
            # a2: one success → 1 * 15 = 15 min
            rid = await db.create_run(a2, prompt="r")
            await db.finish_run(rid, status="succeeded", cost_usd=0.30)

            i1 = await db.agent_impact(a1, 30)
            assert i1["runs"] == 3 and i1["runs_succeeded"] == 2
            assert i1["minutes_saved"] == 60
            assert round(i1["spend_usd"], 2) == 0.35

            org_i = await db.org_impact(org)
            assert org_i["runs_succeeded"] == 3          # 2 + 1
            assert org_i["minutes_saved"] == 75          # 60 + 15
            assert org_i["agents"] == 2
            assert round(org_i["spend_usd"], 2) == 0.65
        finally:
            await db.close()

    _run(scenario)


def test_alerts_and_notifications():
    """U7: run-outcome rules create notifications; unread count + read; org-scoped."""
    _fresh_db()

    async def scenario():
        from app import alerts
        await db.init()
        try:
            org = await db.create_org("Org")
            other = await db.create_org("Other")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            agent = await db.create_agent(
                {"name": "watcher", "prompt": "p"}, org_id=org, owner_id=uid)

            # rebind rules (replace semantics)
            await db.set_agent_alerts(agent, org, [
                {"kind": "on_failure", "enabled": True},
                {"kind": "result_contains", "condition": "ERROR", "enabled": True},
                {"kind": "on_success", "enabled": False},   # disabled: must not fire
            ])
            got = await db.get_agent_alerts(agent)
            assert {r["kind"] for r in got} == {"on_failure", "result_contains", "on_success"}

            # a failing run fires on_failure only
            r1 = await db.create_run(agent, prompt="go")
            await db.finish_run(r1, status="failed", error="boom")
            await alerts.evaluate_run(r1)
            assert await db.unread_count(org) == 1

            # a succeeding run whose output contains ERROR fires result_contains,
            # but NOT the disabled on_success rule
            r2 = await db.create_run(agent, prompt="go")
            await db.finish_run(r2, status="succeeded", result="found an ERROR in row 5")
            await alerts.evaluate_run(r2)
            assert await db.unread_count(org) == 2   # +1 only

            # a clean success fires nothing (on_success disabled, no ERROR in output)
            r3 = await db.create_run(agent, prompt="go")
            await db.finish_run(r3, status="succeeded", result="all good")
            await alerts.evaluate_run(r3)
            assert await db.unread_count(org) == 2   # unchanged

            # notifications are org-scoped
            assert await db.unread_count(other) == 0
            items = await db.list_notifications(org)
            assert len(items) == 2 and items[0]["read"] is False

            # marking all read zeroes the unread count
            await db.mark_notifications_read(org)
            assert await db.unread_count(org) == 0
        finally:
            await db.close()

    _run(scenario)


def test_schedule_miss_alert():
    """U7: schedule_miss notification only when an enabled schedule_miss rule exists."""
    _fresh_db()

    async def scenario():
        from app import alerts
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            a1 = await db.create_agent({"name": "a1", "prompt": "p"}, org_id=org, owner_id=uid)
            a2 = await db.create_agent({"name": "a2", "prompt": "p"}, org_id=org, owner_id=uid)
            await db.set_agent_alerts(a1, org, [{"kind": "schedule_miss", "enabled": True}])
            # a2 has no rule
            await alerts.notify_schedule_miss(a1)
            await alerts.notify_schedule_miss(a2)
            assert await db.unread_count(org) == 1
        finally:
            await db.close()

    _run(scenario)


def test_template_instantiation():
    """U9: a template instantiates a runnable agent with its skills + baseline;
    re-instantiating reuses the same-named skill (idempotent)."""
    _fresh_db()

    async def scenario():
        from app import templates
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")

            catalog = templates.list_templates()
            assert catalog and all("prompt" not in t for t in catalog)   # catalog omits prompt
            tpl = templates.get("news-digest")
            assert tpl and tpl["prompt"] and tpl["skills"]

            async def instantiate():
                aid = await db.create_agent({
                    "name": tpl["name"], "prompt": tpl["prompt"],
                    "system_prompt": tpl.get("system_prompt"),
                    "baseline_minutes": tpl.get("baseline_minutes"),
                    "schedule_kind": tpl.get("schedule_kind", "once"),
                }, org_id=org, owner_id=uid)
                existing = {s["name"]: s["id"] for s in await db.list_skills(org)}
                sids = []
                for s in tpl["skills"]:
                    sid = existing.get(s["name"]) or await db.create_skill(
                        org, s["name"], instructions=s.get("instructions", ""))
                    sids.append(sid)
                await db.set_agent_skills(aid, sids)
                return aid

            aid = await instantiate()
            a = await db.get_owned_agent(aid, org)
            assert a["prompt"] == tpl["prompt"]
            assert a["baseline_minutes"] == tpl["baseline_minutes"]
            assert {s["name"] for s in await db.get_agent_skills(aid)} == {"concise-digest"}

            # second instantiation reuses the skill rather than duplicating it
            await instantiate()
            names = [s["name"] for s in await db.list_skills(org)]
            assert names.count("concise-digest") == 1
        finally:
            await db.close()

    _run(scenario)


def test_register_shape_of_agent_config():
    """create_agent stores model/provider/system_prompt and defaults schedule to once."""
    _fresh_db()

    async def scenario():
        await db.init()
        try:
            org = await db.create_org("Org")
            uid = await db.create_user("u@x.com", org_id=org, password_hash="h")
            aid = await db.create_agent(
                {"name": "cfg", "prompt": "p", "provider": "deepinfra",
                 "model": "zai-org/GLM-5.2", "system_prompt": "be terse"},
                org_id=org, owner_id=uid)
            a = await db.get_owned_agent(aid, org)
            assert a["provider"] == "deepinfra"
            assert a["model"] == "zai-org/GLM-5.2"
            assert a["system_prompt"] == "be terse"
            assert a["schedule_kind"] == "once"
            assert a["enabled"] is True
        finally:
            await db.close()

    _run(scenario)

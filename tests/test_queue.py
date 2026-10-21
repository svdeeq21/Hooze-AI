"""Durable inbound queue: a customer's message must survive a crash/restart, be answered exactly once, and failures must be visible."""
import asyncio

import pytest

from tests.conftest import CUSTOMER
from tests.test_api import PLATFORM, RecPipeline, client, container, wa_body

H = {"x-hooze-secret": "s3cret"}
P = {"x-api-key": PLATFORM}


def url(w): return f"/v1/webhooks/whatsapp/{w.channel['id']}"
def rows(w): return w.db.rows("inbound_events")
def inbound_msgs(w): return [m for m in w.db.rows("messages") if m["direction"] == "inbound"]


class Boom:
    """A pipeline that crashes (as `error` outcomes do) until `ok` is set."""
    def __init__(self): self.ok, self.calls = False, []
    async def handle_inbound(self, channel, inbound, **kw):
        self.calls.append(kw)
        if not self.ok: raise RuntimeError("boom: database timeout")
        return type("O", (), {"status": "replied", "detail": ""})()


async def no_op(*a, **k): return "skipped"


# ── saved before acknowledged ─────────────────────────────────────────
async def test_the_message_is_saved_before_the_webhook_answers_so_a_dead_worker_loses_nothing(re_world):
    w, rec = re_world, RecPipeline()
    c = container(w, pipeline=rec)
    c.inbox.process = no_op                                       # the background task never runs (process killed right after the ack)
    async with client(c) as http:
        r = await http.post(url(w), json=wa_body("hello"), headers=H)
    assert r.json() == {"status": "accepted"} and rec.calls == []
    assert len(rows(w)) == 1 and rows(w)[0]["status"] == "pending" and rows(w)[0]["payload"]["data"]["message"]["conversation"] == "hello"
    w.clock.advance(seconds=60)                                   # next process / recovery loop
    out = await c.inbox.recover()
    assert out["claimed"] == 1 and [i.text for _, i in rec.calls] == ["hello"] and rows(w)[0]["status"] == "done"


async def test_a_provider_redelivery_is_acknowledged_but_processed_once(re_world):
    w, rec = re_world, RecPipeline()
    async with client(container(w, pipeline=rec)) as http:
        a = await http.post(url(w), json=wa_body("hi", msg_id="X1"), headers=H)
        b = await http.post(url(w), json=wa_body("hi", msg_id="X1"), headers=H)
    assert (a.json()["status"], b.json()["status"]) == ("accepted", "duplicate") and len(rows(w)) == 1 and len(rec.calls) == 1


async def test_if_the_message_cannot_be_saved_the_webhook_says_so_instead_of_acknowledging(re_world):
    w, rec = re_world, RecPipeline()
    real = w.db.check_unique

    def down(table, rs, cand):
        if table == "inbound_events": raise RuntimeError("connection reset by peer")
        return real(table, rs, cand)
    w.db.check_unique = down
    async with client(container(w, pipeline=rec)) as http:
        r = await http.post(url(w), json=wa_body("hi"), headers=H)
    assert r.status_code == 503 and rec.calls == [] and rows(w) == [] and "INBOUND_ENQUEUE_FAILED" in w.sink.names()


async def test_noise_is_not_stored_group_chatter_own_messages_and_non_message_events(re_world):
    w = re_world
    async with client(container(w, pipeline=RecPipeline())) as http:
        await http.post(url(w), json=wa_body(event="connection.update"), headers=H)
        own = wa_body("echo"); own["data"]["key"]["fromMe"] = True
        await http.post(url(w), json=own, headers=H)
        grp = wa_body("group chat"); grp["data"]["key"]["remoteJid"] = "1203630@g.us"
        await http.post(url(w), json=grp, headers=H)
    assert rows(w) == []


# ── retries, backoff, giving up, operator retry ───────────────────────
async def test_failures_retry_with_backoff_then_give_up_loudly_and_an_operator_can_retry(re_world):
    w, boom = re_world, Boom()
    c = container(w, pipeline=boom)
    async with client(c) as http:
        await http.post(url(w), json=wa_body("hi"), headers=H)    # attempt 1 runs in the background and fails
        r = rows(w)[0]
        assert r["status"] == "pending" and r["attempts"] == 1 and "boom" in r["last_error"] and "INBOUND_RETRY" in w.sink.names()
        w.clock.advance(seconds=29)
        assert (await c.inbox.recover())["claimed"] == 0           # attempt 1 failed -> 30s backoff: not hammered at 29s ...
        w.clock.advance(seconds=2)
        assert (await c.inbox.recover())["claimed"] == 1           # ... retried at 31s (attempt 2, which fails -> 60s backoff)
        w.clock.advance(seconds=59)
        assert (await c.inbox.recover())["claimed"] == 0
        w.clock.advance(seconds=2)
        assert (await c.inbox.recover())["claimed"] == 1           # attempt 3 at +61s
        for _ in range(2):                                         # attempts 4..5
            w.clock.advance(seconds=700)
            await c.inbox.recover()
        r = rows(w)[0]
        assert r["status"] == "failed" and r["attempts"] == 5 and "INBOUND_FAILED" in w.sink.names()
        w.clock.advance(seconds=700)
        assert (await c.inbox.recover())["claimed"] == 0           # failed rows are never retried automatically
        boom.ok = True                                             # the underlying problem is fixed
        listed = (await http.get("/v1/platform/inbound?status=failed", headers=P)).json()["inbound"]
        assert len(listed) == 1 and listed[0]["attempts"] == 5 and "payload" not in listed[0] and listed[0]["tenant_name"]
        assert (await http.post(f"/v1/platform/inbound/{listed[0]['id']}/retry", headers=P)).status_code == 200
        assert (await http.post(f"/v1/platform/inbound/{listed[0]['id']}/retry", headers=P)).status_code == 404     # no longer failed
    assert rows(w)[0]["status"] == "done"


# ── the important one: crash AFTER the message was stored ────────────
async def test_a_crash_mid_turn_is_recovered_and_the_customer_gets_exactly_one_reply(re_world):
    """First attempt: message stored, then the model blows up (no reply). Replay must answer it — without a second stored copy."""
    w = re_world
    w.llm.script.append(lambda system, prompt: (_ for _ in ()).throw(RuntimeError("model exploded")))
    w.llm.say("Hello Ada, how can we help?")
    c = container(w)
    async with client(c) as http:
        await http.post(url(w), json=wa_body("hello"), headers=H)
    assert len(inbound_msgs(w)) == 1 and w.replies() == [] and rows(w)[0]["status"] == "pending"      # stored, unanswered, queued for retry
    w.clock.advance(seconds=60)
    out = await c.inbox.recover()
    assert out["done"] == 1 and rows(w)[0]["status"] == "done"
    assert w.replies() == ["Hello Ada, how can we help?"]                                               # exactly one reply
    assert len(inbound_msgs(w)) == 1 and "INBOUND_REPLAYED" in w.sink.names() and "TURN_CRASHED" in w.sink.names()


async def test_a_replay_after_the_customer_was_already_answered_never_replies_again(re_world):
    w = re_world
    w.llm.say("Hi Ada!")
    c = container(w)
    async with client(c) as http:
        await http.post(url(w), json=wa_body("hello"), headers=H)
    assert w.replies() == ["Hi Ada!"]
    r = rows(w)[0]                                                 # worker died after replying but before marking done
    r.update(status="processing", locked_until=(w.clock() - __import__("datetime").timedelta(seconds=1)).isoformat())
    w.llm.say("SECOND REPLY THAT MUST NEVER BE SENT")
    out = await c.inbox.recover()
    assert out["claimed"] == 1 and w.replies() == ["Hi Ada!"] and rows(w)[0]["status"] == "done"


async def test_two_workers_racing_for_the_same_message_run_it_once(re_world):
    w, rec = re_world, RecPipeline()
    c = container(w, pipeline=rec)
    eid, _ = await c.inbox.enqueue({"tenant_id": w.tid, "id": w.channel["id"]}, wa_body("race"), "R1")
    res = await asyncio.gather(c.inbox.process(eid), c.inbox.process(eid))
    assert sorted(res) == ["done", "skipped"] and len(rec.calls) == 1


# ── housekeeping ──────────────────────────────────────────────────────
async def test_finished_rows_are_purged_after_the_retention_window_but_open_and_failed_ones_never(re_world):
    w = re_world
    c = container(w, pipeline=RecPipeline())
    now = w.clock()
    import datetime as dt
    for i, (status, age_days) in enumerate([("done", 30), ("done", 1), ("failed", 30), ("pending", 30)]):
        await w.pdb.table("inbound_events").insert({"tenant_id": w.tid, "channel_id": w.channel["id"], "provider_message_id": f"P{i}", "payload": {},
                                                    "status": status, "finished_at": (now - dt.timedelta(days=age_days)).isoformat() if status in ("done", "failed") else None}).execute()
    assert await c.inbox.purge() == 1
    assert sorted((r["provider_message_id"], r["status"]) for r in rows(w)) == [("P1", "done"), ("P2", "failed"), ("P3", "pending")]


# ── connection monitor ────────────────────────────────────────────────
async def test_a_disconnected_whatsapp_number_is_reported_once_per_check_and_recovery_is_noted(re_world):
    w = re_world
    c = container(w, pipeline=RecPipeline())
    state = {"v": "close"}
    async def status(): return state["v"]
    w.adapter.connection_status = status
    out = await c.maintenance.run_once()
    ev = w.sink.find("CHANNEL_DISCONNECTED")
    assert out["channels"]["down"] == 1 and len(ev) == 1 and ev[0]["metadata"]["state"] == "close" and ev[0]["tenant_id"] == w.tid
    await c.maintenance.run_once()
    assert len(w.sink.find("CHANNEL_DISCONNECTED")) == 1           # not re-checked inside the interval
    state["v"] = "open"
    w.clock.advance(seconds=121)
    await c.maintenance.run_once()
    assert len(w.sink.find("CHANNEL_RECOVERED")) == 1


async def test_an_unreachable_evolution_counts_as_disconnected(re_world):
    w = re_world
    c = container(w, pipeline=RecPipeline())
    async def dead(): raise ConnectionError("evolution is down")
    w.adapter.connection_status = dead
    await c.maintenance.run_once()
    assert w.sink.find("CHANNEL_DISCONNECTED")[0]["metadata"]["state"] == "UNREACHABLE"


async def test_one_failing_maintenance_step_never_stops_the_others(re_world):
    w = re_world
    c = container(w, pipeline=RecPipeline())
    async def broken(*a, **k): raise RuntimeError("db hiccup")
    c.inbox.recover = broken
    out = await c.maintenance.run_once()
    assert "MAINTENANCE_STEP_FAILED" in w.sink.names() and "channels" in out and "purged" in out


# ── health + operator visibility ──────────────────────────────────────
async def test_ready_check_is_200_with_queue_depth_and_503_when_the_database_is_unreachable(re_world):
    w = re_world
    c = container(w, pipeline=RecPipeline())
    async with client(c) as http:
        ok = (await http.get("/health/ready")).json()
        assert ok == {"status": "ok", "queue": {"pending": 0, "processing": 0, "failed": 0}}
        def down(name): raise RuntimeError("db down")
        c.pdb.table = down
        r = await http.get("/health/ready")
    assert r.status_code == 503 and r.json() == {"status": "unavailable"}


async def test_overview_shows_queue_counts_and_whether_alerts_are_configured(re_world):
    w = re_world
    c = container(w, pipeline=RecPipeline())
    await w.pdb.table("inbound_events").insert({"tenant_id": w.tid, "channel_id": w.channel["id"], "provider_message_id": "F1", "payload": {}, "status": "failed"}).execute()
    async with client(c) as http:
        ov = (await http.get("/v1/platform/overview", headers=P)).json()
    assert ov["inbound"]["failed"] == 1 and ov["alerts"] == {"configured": False, "destinations": []}


# ── the real startup path ─────────────────────────────────────────────
def test_the_app_starts_the_recovery_loop_on_boot_recovers_old_messages_and_stops_cleanly(re_world, monkeypatch):
    """Boots create_app() the way production does (lifespan, no injected container). The message was saved by a previous process."""
    import datetime as dt, time
    from fastapi.testclient import TestClient
    from hooze.api import app as app_mod
    from hooze.settings import Settings
    w, rec = re_world, RecPipeline()
    c = container(w, pipeline=rec)
    c.settings = Settings(_env_file=None, app_env="development", platform_admin_key=PLATFORM, internal_cron_secret="x", maintenance_enabled=True, inbound_recovery_interval_seconds=1)
    from hooze.alerts import AlertManager
    c.alerts = AlertManager([], w.clock)                             # nothing configured
    c.maintenance.startup_delay = 0.01
    old = (w.clock() - dt.timedelta(minutes=5)).isoformat()
    asyncio.run(w.pdb.table("inbound_events").insert({"tenant_id": w.tid, "channel_id": w.channel["id"], "provider_message_id": "OLD1", "payload": wa_body("left behind"),
                                                      "received_at": old}).execute())

    async def build(settings): return c
    monkeypatch.setattr(app_mod, "build_container", build)
    monkeypatch.setattr(app_mod, "get_settings", lambda: c.settings)
    with TestClient(app_mod.create_app()) as http:
        deadline = time.time() + 5
        while time.time() < deadline and not (rows(w) and rows(w)[0]["status"] == "done"):
            time.sleep(0.05)
        assert rows(w)[0]["status"] == "done" and [i.text for _, i in rec.calls] == ["left behind"]
        assert http.get("/health/ready").json()["status"] == "ok"
    assert "ALERTS_NOT_CONFIGURED" in w.sink.names()                 # exits without error: the loop task was cancelled and awaited

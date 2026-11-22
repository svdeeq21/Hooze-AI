"""Scale fixes (measured with scripts/load_sim.py): rapid-fire messages answered together, the queue never loses a message over the new
optional column, log/usage rows written in background batches, old log rows cleaned up, suspensions take effect immediately."""
import asyncio
from datetime import timedelta

from hooze.kernel.events import BufferedInserter, buffered_db_sink, EventBus
from hooze.runtime.inbox import Inbox
from hooze.runtime.maintenance import purge_events
from hooze.settings import Settings
from tests.conftest import make_world, realestate_config
from tests.test_api import HybridFactory, PLATFORM, client, container

PHONE = "2348055500001"


def body(text: str, msg_id: str, instance: str) -> dict:
    return {"event": "messages.upsert", "instance": instance, "data": {"key": {"remoteJid": f"{PHONE}@s.whatsapp.net", "fromMe": False, "id": msg_id},
                                                                      "pushName": "Ada", "messageType": "conversation", "message": {"conversation": text}}}


def _inbox(w):
    return Inbox(w.pdb, w.pipeline, HybridFactory(w.adapter), w.events, Settings(_env_file=None), w.clock)


async def test_rapid_fire_messages_get_one_answer_that_covers_all_of_them():
    w = await make_world(realestate_config(stuck_after_user_messages=100))
    inbox = _inbox(w)
    ids = []
    for i, text in enumerate(["Hi", "I'm looking for a house", "in Maitama"]):          # all three queued before any is processed
        ids.append((await inbox.enqueue(w.channel, body(text, f"r{i}", w.channel["instance_name"]), f"r{i}", phone=PHONE))[0])
    w.llm.say("Lovely! What budget are you working with for Maitama?")
    results = [await inbox.process(i) for i in ids]
    assert results == ["done", "done", "done"]
    assert w.replies(PHONE) == ["Lovely! What budget are you working with for Maitama?"] and len(w.llm.calls) == 1
    assert "sent 3 messages in a row" in w.llm.calls[0]["prompt"]
    assert [m["content"] for m in w.db.rows("messages") if m["direction"] == "inbound"] == ["Hi", "I'm looking for a house", "in Maitama"]
    assert w.sink.names().count("INBOUND_MERGED") == 2


async def test_without_the_new_column_messages_are_still_saved_and_answered_one_by_one():
    w = await make_world(realestate_config(stuck_after_user_messages=100))
    inbox = _inbox(w)
    real_table = w.pdb.table

    class NoPhoneColumn:
        def __init__(self, q): self.q = q
        def insert(self, row, **k):
            if isinstance(row, dict) and "contact_phone" in row:
                raise Exception('column "contact_phone" of relation "inbound_events" does not exist')
            return self.q.insert(row, **k)
        def __getattr__(self, n): return getattr(self.q, n)
    w.pdb.table = lambda name: NoPhoneColumn(real_table(name)) if name == "inbound_events" else real_table(name)
    eid, new = await inbox.enqueue(w.channel, body("hello", "x1", w.channel["instance_name"]), "x1", phone=PHONE)
    assert new and eid and inbox._phone_column is False
    w.llm.say("Hi there!")
    assert await inbox.process(eid) == "done" and w.replies(PHONE) == ["Hi there!"]


async def test_log_rows_are_written_in_one_batch_off_the_customers_time():
    w = await make_world(realestate_config())
    calls = []
    real_table = w.pdb.table

    def spy(name):
        q = real_table(name)
        orig = q.insert
        def insert(rows, **k):
            calls.append(len(rows) if isinstance(rows, list) else 1)
            return orig(rows, **k)
        q.insert = insert
        return q
    w.pdb.table = spy
    writer = BufferedInserter(w.pdb, "events", flush_every=0.05)
    bus = EventBus([buffered_db_sink(writer)])
    for i in range(25):
        await bus.info("SOMETHING", tenant_id=w.tid, n=i)                               # returns at once: nothing awaited the database
    assert calls == []
    await asyncio.sleep(0.2)
    assert calls == [25] and len([e for e in w.db.rows("events") if e["event"] == "SOMETHING"]) == 25


async def test_a_batch_the_database_keeps_refusing_is_dropped_not_retried_forever():
    w = await make_world(realestate_config())

    class Broken:
        def table(self, name):
            class T:
                def insert(self, rows):
                    class E:
                        async def execute(self): raise RuntimeError("relation does not exist")
                    return E()
            return T()
    writer = BufferedInserter(Broken(), "events", flush_every=0.01)
    writer.add({"event": "X"})
    for _ in range(6):
        await writer.flush()
    assert writer.dropped == 1 and writer._buf == []


async def test_old_log_rows_are_purged_warnings_kept_longer():
    w = await make_world(realestate_config())
    now = w.clock()
    for days, sev in ((5, "INFO"), (40, "INFO"), (40, "WARNING"), (100, "ERROR")):
        await w.pdb.table("events").insert({"event": f"E{days}{sev}", "severity": sev, "metadata": {}, "created_at": (now - timedelta(days=days)).isoformat()}).execute()
    n = await purge_events(w.pdb, 30, now)
    left = {e["event"] for e in w.db.rows("events") if e["event"].startswith("E")}
    assert n == 2 and left == {"E5INFO", "E40WARNING"}


async def test_suspending_a_client_takes_effect_on_the_very_next_message(re_world):
    w = re_world
    w.llm.say("Hi!")
    assert (await w.send("hello")).status == "replied"                                 # tenant row is now cached
    cont = container(w)
    async with client(cont) as http:
        assert (await http.patch(f"/v1/platform/tenants/{w.tid}", json={"status": "suspended"}, headers={"x-api-key": PLATFORM})).status_code == 200
    assert (await w.send("are you there?")).detail == "tenant_inactive"


def test_the_defaults_are_sized_for_bursts():
    s = Settings(_env_file=None)
    assert s.inbound_max_concurrency >= 40 and s.merge_quick_messages and s.event_retention_days == 30

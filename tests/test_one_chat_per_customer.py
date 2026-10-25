"""A customer is remembered across a client's WhatsApp numbers: one conversation, which follows the number they wrote to last."""
from hooze.runtime.merge import merge_contact_conversations, merge_tenant_conversations
from hooze.runtime.outbox import Outbox
from tests.conftest import CUSTOMER, inbound
from tests.fakes import FakeAdapter
from tests.test_api import client, container
from tests.test_operator_api import P, tenant_key
from tests.test_order_presentation import FakeAdapterFactory


async def second_number(w, name="second"):
    return (await w.pdb.table("channels").insert({"tenant_id": w.tid, "kind": "whatsapp_evolution", "instance_name": name, "config": {},
                                                  "secret_hash": "0" * 64, "status": "active"}).execute()).data[0]


async def say(w, channel, text, phone=CUSTOMER):
    return await w.pipeline.handle_inbound(channel, inbound(text, phone))


def convs(w): return w.db.rows("conversations")


async def test_writing_to_a_second_number_continues_the_same_conversation(re_world):
    w = re_world
    b = await second_number(w)
    await say(w, w.channel, "hello, I want a 3 bedroom flat")
    (conv,) = convs(w)
    w.db.rows("conversations")[0]["summary"] = "Wants a 3 bedroom flat"                      # what the assistant already knows
    state_before = conv["state"]
    await say(w, b, "hi again, are you the same people?")
    (conv,) = convs(w)                                                                     # still ONE conversation
    assert conv["channel_id"] == b["id"] and conv["summary"] == "Wants a 3 bedroom flat" and conv["state"] == state_before
    assert len(w.db.rows("contacts")) == 1
    inbound_texts = [m["content"] for m in w.db.rows("messages") if m["direction"] == "inbound"]
    assert inbound_texts == ["hello, I want a 3 bedroom flat", "hi again, are you the same people?"]
    assert {m["conversation_id"] for m in w.db.rows("messages")} == {conv["id"]}           # one transcript, from both numbers


async def test_the_conversation_follows_the_customer_back_and_forth_without_ever_duplicating(re_world):
    w = re_world
    b = await second_number(w)
    for ch in (w.channel, b, w.channel, b, b, w.channel):
        await say(w, ch, "hello")
    assert len(convs(w)) == 1 and convs(w)[0]["channel_id"] == w.channel["id"]


async def test_replies_receipts_and_followups_go_out_from_the_number_the_customer_wrote_to_last(re_world):
    w = re_world
    b = await second_number(w)
    await say(w, w.channel, "hello"); await say(w, b, "hello again")
    class Rec(FakeAdapterFactory):
        used: list = []
        def for_channel(self, channel): self.used.append(channel["id"]); return self.adapter
    rec = Rec(w.adapter); rec.used = []
    await Outbox(w.pdb, rec, w.tid, [], w.events).send(CUSTOMER, "your receipt", conversation_id=convs(w)[0]["id"], record=False)
    assert rec.used == [b["id"]]


async def test_different_customers_keep_their_own_conversations(re_world):
    w = re_world
    b = await second_number(w)
    await say(w, w.channel, "hi", phone="2348011111111"); await say(w, b, "hi", phone="2348022222222")
    assert len(convs(w)) == 2 and {c["channel_id"] for c in convs(w)} == {w.channel["id"], b["id"]}


async def test_a_handed_off_chat_stays_handed_off_when_the_customer_uses_the_other_number(re_world):
    w = re_world
    b = await second_number(w)
    await say(w, w.channel, "hello")
    convs(w)[0]["status"] = "handoff"
    await say(w, b, "anyone there?")
    assert convs(w)[0]["status"] == "handoff" and len(convs(w)) == 1


async def test_customers_who_already_have_one_chat_per_number_are_left_alone_until_merged(re_world):
    w = re_world
    b = await second_number(w)
    await say(w, w.channel, "hello")
    contact = w.db.rows("contacts")[0]
    await w.tdb.table("conversations").insert({"contact_id": contact["id"], "channel_id": b["id"], "state": "DISCOVERY", "status": "active",
                                               "summary": "old chat on number B", "summary_upto": 0, "user_message_count": 0, "message_count": 0}).execute()
    await say(w, b, "hello B"); await say(w, w.channel, "hello A")
    assert len(convs(w)) == 2                                                              # legacy pair untouched on the hot path


# ── merging legacy pairs ───────────────────────────────────────────────
async def legacy_pair(w):
    b = await second_number(w)
    await say(w, w.channel, "message on A")
    contact = w.db.rows("contacts")[0]
    old = convs(w)[0]
    old.update(summary="summary A", summary_upto=2, message_count=4, user_message_count=2, last_inbound_at="2026-10-01T10:00:00+00:00")
    new = (await w.tdb.table("conversations").insert({"contact_id": contact["id"], "channel_id": b["id"], "state": "DISCOVERY", "status": "active",
                                                      "summary": "summary B", "summary_upto": 1, "user_message_count": 1, "message_count": 3,
                                                      "last_inbound_at": "2026-10-20T10:00:00+00:00"}).execute()).data[0]
    await w.tdb.table("messages").insert({"conversation_id": new["id"], "contact_id": contact["id"], "channel_id": b["id"], "direction": "inbound",
                                          "sender_type": "customer", "content": "message on B", "content_type": "text"}).execute()
    await w.tdb.table("nudge_state").insert({"conversation_id": new["id"], "status": "ACTIVE", "step": 1}).execute()
    await w.tdb.table("nudge_state").insert({"conversation_id": old["id"], "status": "ACTIVE", "step": 0}).execute()
    return contact, old, new


async def test_a_dry_run_reports_the_merge_and_changes_nothing(re_world):
    w = re_world
    contact, old, new = await legacy_pair(w)
    before = [dict(r) for r in convs(w)]
    r = await merge_contact_conversations(w.tdb, contact["id"], dry_run=True)
    assert r["keeper"] == new["id"] and r["merged"] == [old["id"]] and [dict(r) for r in convs(w)] == before


async def test_merging_keeps_the_most_recent_chat_and_moves_everything_into_it(re_world):
    w = re_world
    contact, old, new = await legacy_pair(w)
    await w.tdb.table("bookings").insert({"contact_id": contact["id"], "conversation_id": old["id"], "subject": "Inspection", "status": "pending"}).execute()
    await merge_contact_conversations(w.tdb, contact["id"], dry_run=False)
    (conv,) = convs(w)
    assert conv["id"] == new["id"] and conv["summary"] == "summary A\nsummary B" and conv["message_count"] == 7 and conv["user_message_count"] == 3
    assert {m["conversation_id"] for m in w.db.rows("messages")} == {new["id"]} and len(w.db.rows("messages")) >= 2
    assert w.db.rows("bookings")[0]["conversation_id"] == new["id"]
    assert [n["conversation_id"] for n in w.db.rows("nudge_state")] == [new["id"]] and w.db.rows("nudge_state")[0]["step"] == 1
    assert await merge_contact_conversations(w.tdb, contact["id"], dry_run=False) is None            # idempotent


async def test_a_hand_off_on_either_chat_survives_the_merge(re_world):
    w = re_world
    contact, old, new = await legacy_pair(w)
    old["status"] = "handoff"
    await merge_contact_conversations(w.tdb, contact["id"], dry_run=False)
    assert convs(w)[0]["status"] == "handoff"


async def test_the_merge_endpoint_is_platform_only_and_dry_by_default(re_world):
    w = re_world
    contact, old, new = await legacy_pair(w)
    async with client(container(w)) as c:
        assert (await c.post(f"/v1/platform/tenants/{w.tid}/conversations/merge", headers={"x-api-key": await tenant_key(w)})).status_code == 403
        dry = (await c.post(f"/v1/platform/tenants/{w.tid}/conversations/merge", headers=P)).json()
        assert dry["dry_run"] is True and dry["customers_with_duplicates"] == 1 and len(convs(w)) == 2
        done = (await c.post(f"/v1/platform/tenants/{w.tid}/conversations/merge?dry_run=false", headers=P)).json()
    assert done["conversations_removed"] == 1 and len(convs(w)) == 1

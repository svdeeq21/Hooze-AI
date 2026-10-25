"""One customer's data can be exported and erased without destroying the business's sales records; old chats can expire."""
import json
from datetime import timedelta

from hooze.privacy import apply_retention, digits, erase_customer, export_customer
from tests.conftest import CUSTOMER, STAFF
from tests.test_api import client, container
from tests.test_operator_api import P, tenant_key
from tests.test_ordering import _pending, order

OTHER = "2348055555555"


async def paid_customer(w):
    """CUSTOMER chatted, ordered for delivery and paid. Another customer also exists, and a raw webhook for each sits in the queue."""
    await _pending(w)
    o = order(w)[0]
    await w.tdb.table("orders").update({"delivery_address": "12 Allen Avenue, Ikeja", "delivery_area": "Ikeja"}).eq("id", o["id"]).execute()
    ref = w.db.rows("payments")[0]["reference"]
    await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00})
    await w.send("thanks!", phone=OTHER)
    for who in (CUSTOMER, OTHER):
        await w.pdb.table("inbound_events").insert({"tenant_id": w.tid, "channel_id": w.channel["id"], "provider_message_id": f"Q-{who}",
                                                    "payload": {"data": {"key": {"remoteJid": f"{who}@s.whatsapp.net"}, "message": {"conversation": "private words"}}},
                                                    "status": "done"}).execute()


async def test_export_returns_everything_held_about_the_customer(food_world):
    w = food_world
    await paid_customer(w)
    d = await export_customer(w.tdb, f"+{CUSTOMER}")                                  # formatting of the number does not matter
    assert d["contact"]["phone"] == CUSTOMER and d["messages"] and d["orders"][0]["delivery_address"] == "12 Allen Avenue, Ikeja"
    assert d["payments"][0]["amount_minor"] == 9_000_00 and d["order_items"]
    assert await export_customer(w.tdb, "2340000000000") is None


async def test_a_dry_run_erasure_counts_but_changes_nothing(food_world):
    w = food_world
    await paid_customer(w)
    snapshot = {t: [dict(r) for r in w.db.rows(t)] for t in ("contacts", "messages", "orders", "conversations", "inbound_events")}
    r = await erase_customer(w.tdb, CUSTOMER, dry_run=True)
    assert r["dry_run"] and r["messages"] > 0 and r["conversations"] == 1 and r["queued_webhooks"] == 1 and r["orders_kept_but_scrubbed"] == 1
    assert {t: [dict(x) for x in w.db.rows(t)] for t in snapshot} == snapshot


async def test_erasure_removes_the_person_but_keeps_the_sales_and_payment_records(food_world):
    w = food_world
    await paid_customer(w)
    other_conv = next(c["id"] for c in w.db.rows("conversations") if c["contact_id"] == next(x["id"] for x in w.db.rows("contacts") if x["phone"] == OTHER))
    other_before = [m["content"] for m in w.db.rows("messages") if m["conversation_id"] == other_conv]
    assert any(m["direction"] == "outbound" and not m.get("contact_id") for m in w.db.rows("messages"))       # replies carry no contact_id: they must still be erased
    r = await erase_customer(w.tdb, CUSTOMER, dry_run=False)
    assert r["dry_run"] is False
    gone = next(c for c in w.db.rows("contacts") if c["phone"].startswith("erased-"))
    assert gone["display_name"] is None and gone["attributes"] == {} and gone["opted_out"] is True
    assert CUSTOMER not in json.dumps(w.db.rows("contacts")) and CUSTOMER not in json.dumps(w.db.rows("messages"))
    assert [m for m in w.db.rows("messages") if m.get("contact_id") == gone["id"]] == [] and [c for c in w.db.rows("conversations") if c["contact_id"] == gone["id"]] == []
    assert {m["conversation_id"] for m in w.db.rows("messages")} == {other_conv}                                  # every remaining message belongs to the other customer
    o = order(w)[0]                                                                    # the order and its money are still there...
    assert o["status"] == "PAID" and o["subtotal_minor"] == 9_000_00 and o["delivery_address"] is None and o["delivery_area"] is None   # ...without the address
    assert w.db.rows("payments")[0]["amount_minor"] == 9_000_00 and w.db.rows("order_items")
    assert [e["payload"]["data"]["key"]["remoteJid"] for e in w.db.rows("inbound_events")] == [f"{OTHER}@s.whatsapp.net"]    # only the OTHER customer's queue row remains
    assert [m["content"] for m in w.db.rows("messages")] == other_before                                          # nobody else was touched
    assert await erase_customer(w.tdb, CUSTOMER, dry_run=False) is None                # the number no longer matches anyone


async def test_the_same_number_can_chat_again_after_erasure_as_a_brand_new_customer(food_world):
    w = food_world
    await paid_customer(w)
    await erase_customer(w.tdb, CUSTOMER, dry_run=False)
    await w.send("hello again")
    fresh = next(c for c in w.db.rows("contacts") if c["phone"] == CUSTOMER)
    assert fresh["opted_out"] is False and len([c for c in w.db.rows("contacts") if c["phone"].startswith("erased-")]) == 1


async def test_bookings_and_follow_up_state_are_erased_too(re_world):
    w = re_world
    from tests.test_capabilities import _request
    await _request(w)
    cid = w.db.rows("contacts")[0]["id"]
    await w.tdb.table("nudge_state").insert({"conversation_id": w.db.rows("conversations")[0]["id"], "status": "ACTIVE", "step": 1}).execute()
    assert w.db.rows("bookings") and w.db.rows("nudge_state")
    await erase_customer(w.tdb, w.db.rows("contacts")[0]["phone"], dry_run=False)
    assert w.db.rows("bookings") == [] and w.db.rows("nudge_state") == [] and w.db.rows("contacts")[0]["id"] == cid


# ── API ────────────────────────────────────────────────────────────────
async def test_only_admin_or_platform_keys_can_export_or_erase_and_erasure_is_dry_by_default(food_world):
    w = food_world
    await paid_customer(w)
    staff = {"x-api-key": await tenant_key(w)}                                         # default role: staff
    async with client(container(w)) as c:
        assert (await c.get(f"/v1/tenants/{w.tid}/customers/{CUSTOMER}/export", headers=staff)).status_code == 403
        assert (await c.delete(f"/v1/tenants/{w.tid}/customers/{CUSTOMER}?dry_run=false", headers=staff)).status_code == 403
        assert (await c.get(f"/v1/tenants/{w.tid}/customers/{CUSTOMER}/export", headers=P)).json()["contact"]["phone"] == CUSTOMER
        assert (await c.get(f"/v1/tenants/{w.tid}/customers/2340000000000/export", headers=P)).status_code == 404
        dry = await c.delete(f"/v1/tenants/{w.tid}/customers/{CUSTOMER}", headers=P)
        assert dry.json()["dry_run"] is True and len(w.db.rows("messages")) > 0
        done = await c.delete(f"/v1/tenants/{w.tid}/customers/{CUSTOMER}?dry_run=false", headers=P)
    assert done.status_code == 200 and "CUSTOMER_ERASED" in w.sink.names()
    assert CUSTOMER not in json.dumps([e for e in w.sink.events if e["event"] == "CUSTOMER_ERASED"])      # the audit event never records the number


async def test_erasing_one_clients_customer_never_touches_another_clients_customer_with_the_same_number(food_world, re_world):
    w, other = food_world, re_world
    await w.send("hi"); await other.send("hi")
    await erase_customer(w.tdb, CUSTOMER, dry_run=False)
    assert [c["phone"] for c in other.db.rows("contacts") if c["tenant_id"] == other.tid] == [CUSTOMER]


# ── retention ──────────────────────────────────────────────────────────
async def test_retention_deletes_only_old_messages_and_clears_summaries_built_from_them(food_world):
    w = food_world
    await w.send("an old message")
    old_conv = w.db.rows("conversations")[0]
    old_conv["summary"], old_conv["summary_upto"] = "customer asked about boxes", 3
    w.clock.advance(days=100)
    await w.send("a new message", phone=OTHER)
    new_conv = next(c for c in w.db.rows("conversations") if c["id"] != old_conv["id"])
    new_conv["summary"] = "recent chat"
    r = await apply_retention(w.pdb, 90, w.clock())
    texts = [m["content"] for m in w.db.rows("messages")]
    assert r == {"enabled": True, "messages": r["messages"], "conversations_cleared": 1} and r["messages"] > 0
    assert "an old message" not in texts and "a new message" in texts
    assert old_conv["summary"] == "" and old_conv["summary_upto"] == 0 and new_conv["summary"] == "recent chat"
    assert w.db.rows("contacts") and w.db.rows("orders") is not None                  # people and sales records are not retention's business


async def test_retention_is_off_by_default_and_when_set_to_zero(food_world):
    w = food_world
    await w.send("old"); w.clock.advance(days=1000)
    assert await apply_retention(w.pdb, 0, w.clock()) == {"enabled": False, "messages": 0, "conversations_cleared": 0}
    assert any(m["content"] == "old" for m in w.db.rows("messages"))


async def test_the_background_maintenance_pass_applies_the_retention_rule_when_it_is_switched_on(food_world):
    from hooze.runtime.maintenance import Maintenance
    w = food_world
    await w.send("old"); w.clock.advance(days=40)
    c = container(w)
    c.settings.message_retention_days = 30
    out = await c.maintenance.run_once()
    assert out["retention"]["messages"] > 0 and not any(m["content"] == "old" for m in w.db.rows("messages"))
    c.settings.message_retention_days = 0
    c.maintenance._last_retention = None
    assert "retention" not in await c.maintenance.run_once()


def test_digits_ignores_formatting():
    assert digits("+234 801-111 1111") == "2348011111111"

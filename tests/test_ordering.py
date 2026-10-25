"""Ordering + availability + payments: the money path. Each test names the savoury/pd incident it guards."""
import asyncio, re

import pytest

from hooze.capabilities.availability import resolve_open, to_local, within_hours
from hooze.capabilities.ordering import line_total_minor
from hooze.payments.base import fmt_money
from tests.conftest import CUSTOMER, STAFF

BOX = "Loaded Potato Box"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def order(w, **where): return w.db.rows("orders", **where)
def items(w): return w.db.rows("order_items")


async def add(w, product, mods=(), qty=None, say="Sure!"):
    args = {"product_name": product, "modifier_names": list(mods)}
    if qty is not None: args["quantity"] = qty
    w.llm.say(say, ("add_product", args))
    return await w.send(f"add {product}")


async def ready_cart(w, fulfil=("PICKUP", None)):
    await add(w, BOX, ["Cheese", "Large"], qty=2)
    w.llm.say("ok", ("set_fulfillment", {"method": fulfil[0], "delivery_address": fulfil[1]}))
    await w.send(fulfil[0].lower())


# ── pure pricing / formatting ──────────────────────────────────────────
def test_line_total_is_integer_minor_units_and_validates_quantity():
    assert line_total_minor(3_500_00, [1_000_00, 300_00], 2) == 9_600_00
    with pytest.raises(ValueError): line_total_minor(100, [], 0)


def test_fmt_money():
    assert fmt_money(350000) == "₦3,500" and fmt_money(350050) == "₦3,500.50" and fmt_money(0) == "₦0"


# ── menu / prompt ──────────────────────────────────────────────────────
async def test_reading_the_menu_creates_no_order_rows(food_world):
    """Found by the 'writes nothing' test: context() used to call get_cart(), creating an empty CART for every visitor."""
    w = food_world
    for i in range(3): await w.send(f"what do you have {i}?")
    assert order(w) == []


async def test_menu_context_shows_fixed_prices_and_never_leaks_ids(food_world):
    w = food_world
    await w.send("what do you have?")
    system = w.llm.calls[0]["system"]
    assert "Loaded Potato Box (₦3,500)" in system and "Large (+₦1,000)" in system and "Chapman (₦800)" in system
    assert "Size [required, pick exactly 1]" in system and "up to 3 included free" in system
    assert "add_product" in system and "checkout" in system
    assert not UUID_RE.search(w.llm.calls[0]["prompt"]) and not UUID_RE.search(system)      # short line refs, never UUIDs


# ── draft item persistence (savoury's fix) ─────────────────────────────
async def test_partial_item_is_persisted_and_customer_is_told_every_remaining_choice(food_world):
    w = food_world
    await add(w, BOX, ["Cheese", "Bacon"], qty=2)
    msg = w.last_reply()
    assert "Cheese, Bacon" in msg and "Size" in msg and "Regular" in msg and "Large" in msg      # deterministic, from the catalog
    assert items(w) == [] and order(w)[0]["draft"]["quantity"] == 2 and len(order(w)[0]["draft"]["modifier_ids"]) == 2


async def test_second_answer_merges_into_the_draft_and_keeps_quantity(food_world):
    """Regression: 'Large' arrives as a follow-up add_product with no quantity — the earlier '2 boxes' must survive."""
    w = food_world
    await add(w, BOX, ["Cheese", "Bacon"], qty=2)
    await add(w, BOX, ["Large"])                                   # quantity omitted
    (it,) = items(w)
    assert it["quantity"] == 2
    assert {m["name"] for m in it["modifiers"]} == {"Cheese", "Bacon", "Large"}
    assert it["line_total_minor"] == (3_500_00 + 1_000_00) * 2 and order(w)[0]["subtotal_minor"] == 9_000_00 and order(w)[0]["draft"] is None


async def test_single_select_group_replaces_rather_than_accumulates(food_world):
    w = food_world
    await add(w, BOX, ["Regular"], qty=1)
    (a,) = items(w)
    assert [m["name"] for m in a["modifiers"]] == ["Regular"]
    await add(w, BOX, ["Cheese", "Regular", "Large"], qty=1)      # both sizes named in one breath: last wins, only one kept
    sizes = [m["name"] for m in items(w)[-1]["modifiers"] if m["name"] in ("Regular", "Large")]
    assert len(sizes) == 1


async def test_unknown_modifier_is_reported_not_silently_dropped(food_world):
    w = food_world
    await add(w, BOX, ["Guacamole", "Large"], qty=1)
    assert "Guacamole" in w.last_reply() and "couldn't find" in w.last_reply()
    assert len(items(w)) == 1 and "Guacamole" not in [m["name"] for m in items(w)[0]["modifiers"]]


async def test_unknown_product_gets_an_honest_message_and_writes_nothing(food_world):
    w = food_world
    await add(w, "Jollof Rice Bowl", say="Added it to your cart!")
    assert "couldn't match" in w.last_reply() and "Added it" not in w.last_reply()
    assert order(w) == [] and items(w) == [] and "LLM_REFERENCED_UNKNOWN_PRODUCT" in w.sink.names()


async def test_included_limit_is_enforced_with_an_extras_hint_and_persists_nothing(food_world):
    w = food_world
    await add(w, BOX, ["Cheese", "Bacon", "Chicken", "Sausage", "Regular"])
    assert "Toppings includes up to 3" in w.last_reply() and "Extras" in w.last_reply()
    assert items(w) == [] and (order(w) == [] or order(w)[0]["draft"] is None)


async def test_an_unearned_added_claim_is_stripped_from_the_reply(food_world):
    """savoury: 'Adding that to your cart!' when nothing was added."""
    w = food_world
    w.llm.say("I've added a Chapman to your cart!", intent="order")
    await w.send("i want a chapman")
    assert "added" not in w.last_reply().lower() and items(w) == []


async def test_next_step_is_computed_from_post_action_cart_state(food_world):
    w = food_world
    await add(w, "Chapman", say="Great choice.")
    assert w.last_reply() == "Great choice. Want to add anything else, or are you ready to checkout? Also, pickup or delivery?"
    assert w.db.rows("conversations")[0]["state"] == "ORDERING"


# ── cart lifecycle ─────────────────────────────────────────────────────
async def test_remove_and_change_quantity_by_line_number(food_world):
    w = food_world
    await add(w, "Chapman", qty=1); await add(w, BOX, ["Regular"], qty=1)
    w.llm.say("Removed.", ("remove_item", {"line": 1})); await w.send("remove the chapman")
    assert [i["product_name"] for i in items(w)] == [BOX] and order(w)[0]["subtotal_minor"] == 3_500_00
    w.llm.say("x", ("set_quantity", {"line": 1, "quantity": 3})); await w.send("make it 3")
    assert order(w)[0]["subtotal_minor"] == 10_500_00
    w.llm.say("x", ("remove_item", {"line": 9})); await w.send("remove #9")
    assert "don't see item #9" in w.last_reply()
    w.llm.say("x", ("set_quantity", {"line": 1, "quantity": 0})); await w.send("zero")
    assert "at least 1" in w.last_reply()


async def test_stale_cart_expires_but_every_mutation_keeps_it_alive(food_world):
    """savoury: set_fulfillment_details never bumped updated_at, so a cart expired mid-conversation."""
    w = food_world
    await add(w, "Chapman", qty=1)
    w.clock.advance(hours=2)                                   # 14:00 Lagos
    w.llm.say("ok", ("set_fulfillment", {"method": "PICKUP"})); await w.send("pickup")
    w.clock.advance(hours=3)                                   # 17:00: 5h since creation, 3h since last mutation (limit is 4h)
    await add(w, "Chapman", qty=1)
    assert len(order(w, status="CART")) == 1 and len(items(w)) == 2 and order(w, status="EXPIRED") == []
    w.clock.advance(hours=20)                                  # next day 13:00 (inside opening hours): genuinely abandoned
    await add(w, "Chapman", qty=1)
    assert len(order(w, status="EXPIRED")) == 1 and len(order(w, status="CART")) == 1


async def test_delivery_requires_an_address_and_pickup_clears_it(food_world):
    w = food_world
    await add(w, "Chapman", qty=1)
    w.llm.say("ok", ("set_fulfillment", {"method": "delivery"})); await w.send("deliver")
    assert "What address" in w.last_reply() and order(w)[0].get("fulfillment_method") is None
    w.llm.say("ok", ("set_fulfillment", {"method": "delivery", "delivery_address": "12 Aminu Kano Crescent"})); await w.send("12 Aminu Kano")
    assert order(w)[0]["fulfillment_method"] == "DELIVERY" and order(w)[0]["delivery_address"] == "12 Aminu Kano Crescent"
    w.llm.say("ok", ("set_fulfillment", {"method": "PICKUP"})); await w.send("actually pickup")
    assert order(w)[0]["delivery_address"] is None
    w.llm.say("ok", ("set_fulfillment", {"method": "teleport"})); await w.send("teleport")
    assert "pickup or delivery" in w.last_reply()


# ── checkout ───────────────────────────────────────────────────────────
async def test_checkout_guards_empty_cart_and_missing_fulfilment(food_world):
    w = food_world
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "cart is empty" in w.last_reply()
    await add(w, "Chapman", qty=1)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "pickup or delivery" in w.last_reply() and order(w)[0]["status"] == "CART"


async def test_checkout_charges_food_only_in_kobo_and_sends_the_link(food_world):
    w = food_world
    await ready_cart(w, ("DELIVERY", "5 Wuse Zone 2"))
    w.llm.say("Here you go!", ("checkout", {})); await w.send("pay now")
    o = order(w)[0]
    assert o["status"] == "PAYMENT_PENDING" and o["subtotal_minor"] == 9_000_00
    (p,) = w.db.rows("payments")
    assert p["amount_minor"] == 9_000_00 and p["reference"].startswith(f"{w.tid[:8]}_{o['id']}_") and p["status"] == "pending"
    reply = w.last_reply()
    assert "₦9,000" in reply and p["authorization_url"] in reply and "food only" in reply and "Here you go" not in reply   # override wins
    assert w.db.rows("conversations")[0]["state"] == "CHECKOUT"


async def test_checkout_provider_failure_reverts_to_cart_and_tells_the_customer(food_world):
    w = food_world
    await ready_cart(w)
    w.payments.fail_init = True
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "couldn't start checkout" in w.last_reply() and order(w)[0]["status"] == "CART" and w.db.rows("payments") == []
    assert "CHECKOUT_INIT_FAILED" in w.sink.names()


# ── payment-pending gate (LLM bypassed) ────────────────────────────────
async def _pending(w):
    await ready_cart(w)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    w.llm.calls.clear()


async def test_while_payment_is_pending_the_llm_is_bypassed_and_the_link_is_repeated(food_world):
    w = food_world
    await _pending(w)
    out = await w.send("hello? did it work?")
    assert out.status == "gated" and w.llm.calls == []
    assert "payment in progress" in w.last_reply() and "https://pay.test/" in w.last_reply()


async def test_cancel_during_pending_payment_is_code_driven(food_world):
    w = food_world
    await _pending(w)
    await w.send("cancel")
    assert "cancelled" in w.last_reply().lower() and order(w)[0]["status"] == "CANCELLED" and w.llm.calls == []


async def test_cancel_plus_same_again_rebuilds_the_order_from_the_old_one(food_world):
    w = food_world
    await _pending(w)
    await w.send("cancel that and do the same order again")
    old, new = order(w, status="CANCELLED")[0], order(w, status="CART")[0]
    assert new["subtotal_minor"] == old["subtotal_minor"] == 9_000_00 and new["fulfillment_method"] == "PICKUP"
    assert len([i for i in items(w) if i["order_id"] == new["id"]]) == 1 and "Total: ₦9,000" in w.last_reply()


# ── payment confirmation: exactly once ─────────────────────────────────
async def test_confirm_payment_marks_paid_notifies_staff_and_customer_once(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    n_staff, n_cust = len(w.replies(STAFF)), len(w.replies())
    res = await w.cap("ordering").confirm_payment(ref, await w.payments.verify(ref))
    assert res["status"] == "PAID" and order(w)[0]["paid_at"] and w.db.rows("payments")[0]["status"] == "success"
    assert len(w.replies(STAFF)) == n_staff + 1 and "New paid order" in w.last_reply(STAFF) and "PICKUP" in w.last_reply(STAFF)
    assert len(w.replies()) == n_cust + 1 and "Payment received" in w.last_reply()


async def test_redelivered_and_concurrent_webhooks_confirm_exactly_once(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    before = len(w.replies(STAFF))
    results = await asyncio.gather(*[w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00}) for _ in range(5)])
    assert sum(r is not None for r in results) == 1                        # one winner
    assert len(w.replies(STAFF)) == before + 1 and "PAYMENT_DUPLICATE_IGNORED" in w.sink.names()


async def test_underpayment_is_rejected_even_though_status_says_success(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    assert await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 1_00}) is None
    assert order(w)[0]["status"] == "PAYMENT_PENDING" and "PAYMENT_AMOUNT_MISMATCH" in w.sink.names()


async def test_failed_or_unknown_payments_never_mark_an_order_paid(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    assert await w.cap("ordering").confirm_payment(ref, {"status": "failed", "amount_minor": 9_000_00}) is None
    assert await w.cap("ordering").confirm_payment("nope_123", {"status": "success", "amount_minor": 1}) is None
    assert order(w)[0]["status"] == "PAYMENT_PENDING" and "PAYMENT_UNKNOWN_REFERENCE" in w.sink.names()


async def test_abandoned_pending_orders_expire_on_tick(food_world):
    w = food_world
    await _pending(w)
    w.clock.advance(minutes=45)
    assert (await w.cap("ordering").tick())["expired"] == 1 and order(w)[0]["status"] == "EXPIRED"


# ── availability ───────────────────────────────────────────────────────
def test_within_hours_handles_overnight_windows():
    from datetime import datetime, timezone
    at = lambda h, m=0: datetime(2026, 9, 24, h, m, tzinfo=timezone.utc)
    assert within_hours(["09:00", "21:00"], at(12)) and not within_hours(["09:00", "21:00"], at(22))
    assert within_hours(["18:00", "02:00"], at(23)) and within_hours(["18:00", "02:00"], at(1)) and not within_hours(["18:00", "02:00"], at(12))


def test_precedence_paused_over_closed_over_hours_and_no_hours_means_always_open():
    from datetime import datetime, timezone
    noon = to_local(datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc), "Africa/Lagos")
    hours = {"default": ["09:00", "21:00"]}
    assert resolve_open({"status": "PAUSED", "pause_message": "Out of gas"}, hours, noon) == (False, "Out of gas")
    assert resolve_open({"status": "CLOSED"}, hours, noon)[0] is False
    assert resolve_open(None, hours, noon) == (True, None) and resolve_open(None, {}, noon) == (True, None)
    assert resolve_open(None, {"default": ["09:00", "21:00"], "3": None}, noon)[0] is False            # Thursday closed


async def test_outside_opening_hours_bypasses_llm_and_does_not_repeat_itself(food_world):
    w = food_world
    w.clock.advance(hours=10, minutes=30)                                   # 22:30 in Lagos
    out = await w.send("hi")
    assert out.status == "gated" and "opening hours" in w.last_reply() and w.llm.calls == []
    n = len(w.replies())
    await w.send("hello??")                                                 # real transcript bug: identical closed notice spammed
    assert len(w.replies()) == n


async def test_staff_can_pause_and_resume_orders(food_world):
    w = food_world
    await w.send("pause orders: out of gas", phone=STAFF)
    assert "paused" in w.last_reply(STAFF).lower()
    out = await w.send("hi")
    assert out.status == "gated" and w.llm.calls == []
    await w.send("resume", phone=STAFF)
    w.llm.say("Welcome back!")
    assert (await w.send("hi again")).status == "replied"


async def test_sold_out_hides_items_and_back_in_stock_restores_them(food_world):
    w = food_world
    assert "sold out" in (await w.send("chapman sold out", phone=STAFF)).reply
    await w.send("what do you have?")
    assert "Chapman" not in w.llm.calls[-1]["system"]
    await add(w, "Chapman"); assert "couldn't match" in w.last_reply()
    await w.send("bacon is out", phone=STAFF)
    await w.send("what now?"); assert "Bacon" not in w.llm.calls[-1]["system"] and "Cheese" in w.llm.calls[-1]["system"]
    await w.send("chapman back in stock", phone=STAFF); await w.send("bacon is available", phone=STAFF)
    await w.send("and now?"); assert "Chapman" in w.llm.calls[-1]["system"] and "Bacon" in w.llm.calls[-1]["system"]
    assert "couldn't find" in (await w.send("unicorn sold out", phone=STAFF)).reply

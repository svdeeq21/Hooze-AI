"""Money paths: a failed write mid-confirmation must be recoverable by the provider's retry, a checkout retry must never reuse a reference
Paystack has already seen, and a second (different) payment for an already-paid order must raise an alert instead of vanishing."""
import contextlib

import pytest

from tests.conftest import STAFF
from tests.fakes import FakeQuery
from tests.test_ordering import _pending, order

AMOUNT = {"status": "success", "amount_minor": 9_000_00}


@contextlib.contextmanager
def fail_once(table: str, op: str):
    """Make the first `op` on `table` raise (like a dropped DB connection), then behave normally."""
    real, state = FakeQuery.execute, {"left": 1}
    async def flaky(self):
        if self.t == table and self._op == op and state["left"]:
            state["left"] -= 1
            raise RuntimeError("connection reset")
        return await real(self)
    FakeQuery.execute = flaky
    try:
        yield
    finally:
        FakeQuery.execute = real


async def confirm(w, ref, verified=AMOUNT):
    return await w.cap("ordering").confirm_payment(ref, verified)


# ── item 6: the two writes are ordered so a retry finishes the job ─────
async def test_failure_recording_the_payment_leaves_the_order_waiting_and_the_retry_completes_it(food_world):
    w = food_world
    await _pending(w)
    ref, staff_before = w.db.rows("payments")[0]["reference"], len(w.replies(STAFF))
    with fail_once("payments", "update"):
        with pytest.raises(RuntimeError):
            await confirm(w, ref)
    assert order(w)[0]["status"] == "PAYMENT_PENDING" and len(w.replies(STAFF)) == staff_before      # nothing half-done
    assert await confirm(w, ref) is not None                                                           # provider retry succeeds
    assert order(w)[0]["status"] == "PAID" and w.db.rows("payments")[0]["status"] == "success" and len(w.replies(STAFF)) == staff_before + 1


async def test_failure_marking_the_order_paid_is_finished_by_the_retry_and_staff_and_customer_are_told_once(food_world):
    w = food_world
    await _pending(w)
    ref, staff_before, cust_before = w.db.rows("payments")[0]["reference"], len(w.replies(STAFF)), len(w.replies())
    with fail_once("orders", "update"):
        with pytest.raises(RuntimeError):
            await confirm(w, ref)
    assert w.db.rows("payments")[0]["status"] == "success" and order(w)[0]["status"] == "PAYMENT_PENDING"   # the recoverable half-state
    assert await confirm(w, ref) is not None                                                                  # the retry must NOT call it a duplicate
    assert order(w)[0]["status"] == "PAID" and len(w.replies(STAFF)) == staff_before + 1 and len(w.replies()) == cust_before + 1
    assert await confirm(w, ref) is None and len(w.replies(STAFF)) == staff_before + 1                       # a further redelivery is a no-op


async def test_a_payment_row_left_pending_by_the_old_order_of_writes_is_healed_by_a_redelivery(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    await confirm(w, ref)
    w.db.rows("payments")[0]["status"] = "pending"                                                           # state the old code could leave behind
    staff = len(w.replies(STAFF))
    assert await confirm(w, ref) is None
    assert w.db.rows("payments")[0]["status"] == "success" and len(w.replies(STAFF)) == staff                # healed, nobody notified again
    assert "PAYMENT_DOUBLE_PAYMENT" not in w.sink.names()


# ── item 7: every checkout attempt gets its own reference ──────────────
async def test_a_checkout_retry_after_paystack_accepted_the_first_attempt_uses_a_new_reference(food_world):
    w = food_world
    seen: set = set()
    real_init = w.payments.initialize
    async def strict_init(*, reference, **kw):                                  # Paystack refuses a reference it has already seen
        if reference in seen:
            raise RuntimeError("Duplicate Transaction Reference")
        seen.add(reference)
        return await real_init(reference=reference, **kw)
    w.payments.initialize = strict_init
    from tests.test_ordering import ready_cart
    await ready_cart(w)
    with fail_once("payments", "insert"):                                       # Paystack accepted it, then OUR write failed
        w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "couldn't start checkout" in w.last_reply() and order(w)[0]["status"] == "CART" and w.db.rows("payments") == []
    w.llm.say("x", ("checkout", {})); await w.send("pay again")
    (p,) = w.db.rows("payments")
    assert order(w)[0]["status"] == "PAYMENT_PENDING" and p["authorization_url"] in w.last_reply()
    assert len(seen) == 2 and p["reference"] in seen                            # two different references were used


# ── a second, different payment for an order that is already paid ──────
async def _second_payment(w, ref2="second-ref"):
    first = w.db.rows("payments")[0]
    await w.tdb.table("payments").insert({"order_id": first["order_id"], "provider": "paystack", "reference": ref2, "status": "pending",
                                          "amount_minor": first["amount_minor"], "currency": "NGN", "authorization_url": "https://pay.test/second"}).execute()
    return ref2


async def test_paying_two_different_links_for_one_order_alerts_the_business_to_refund_the_second(food_world):
    w = food_world
    await _pending(w)
    ref1 = w.db.rows("payments")[0]["reference"]
    ref2 = await _second_payment(w)
    assert await confirm(w, ref1) is not None
    staff = len(w.replies(STAFF))
    assert await confirm(w, ref2) is None
    assert "PAYMENT_DOUBLE_PAYMENT" in w.sink.names() and len(w.replies(STAFF)) == staff + 1
    assert "TWICE" in w.last_reply(STAFF) and order(w)[0]["status"] == "PAID"
    staff = len(w.replies(STAFF))
    assert await confirm(w, ref2) is None and len(w.replies(STAFF)) == staff                                # the same alert is not repeated on redelivery


async def test_redelivery_of_the_same_payment_is_not_mistaken_for_a_double_payment(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    await confirm(w, ref); await confirm(w, ref); await confirm(w, ref)
    assert "PAYMENT_DOUBLE_PAYMENT" not in w.sink.names()

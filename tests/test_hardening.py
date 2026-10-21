"""Regression tests for defects found when the codebase was re-audited after the first real-infrastructure run.

Each test names the failure it guards. All of these were RED against the code as handed over.
  1. money   : a customer who paid after checkout expired / was cancelled was silently ignored ("duplicate")
  2. data    : any DB error while claiming an inbound message was reported as a duplicate -> message silently dropped
  3. context : nudges / booking confirmations / receipts were sent but never written to the transcript
  4. go-live : activating a config archived the old one BEFORE the new one was safely stored (not atomic)
  5. time    : a naive confirmation time was stored as UTC instead of the tenant's local time
  6. latency : a failing provider was re-tried (and timed out) on EVERY turn; embed + interpret paid it twice
  7. cron    : overlapping /internal/tick runs could double-process (nudges had a 20-message incident)
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from hooze.config_engine.activation import activate
from hooze.kernel.db import is_unique_violation
from hooze.llm.gateway import LLMGateway, LLMUnavailable
from hooze.runtime.scheduler import run_tick
from tests.conftest import CUSTOMER, STAFF, food_config
from tests.fakes import FakeAdapterFactory
from tests.test_capabilities import _request, bookings, idle_discovery, set_nudge_cfg, tick as nudge_tick
from tests.test_ordering import _pending


# ── 1. money ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("how, prior", [("expired", "EXPIRED"), ("cancelled", "CANCELLED")])
async def test_a_payment_after_expiry_or_cancel_is_honoured_and_flagged_never_swallowed(food_world, how, prior):
    """The customer's money was taken. Silently logging 'duplicate' and doing nothing is the worst possible outcome."""
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    if how == "expired":
        w.clock.advance(minutes=45)
        await w.cap("ordering").tick()
    else:
        await w.send("cancel")
    assert w.db.rows("orders")[0]["status"] == prior

    staff_before, cust_before = len(w.replies(STAFF)), len(w.replies())
    res = await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00})

    assert res and w.db.rows("orders")[0]["status"] == "PAID" and w.db.rows("payments")[0]["status"] == "success"
    assert f"PAYMENT_AFTER_{prior}" in w.sink.names()
    assert len(w.replies(STAFF)) == staff_before + 1 and how in w.last_reply(STAFF).lower() and "refund" in w.last_reply(STAFF).lower()
    assert len(w.replies()) == cust_before + 1 and "Payment received" in w.last_reply()
    # and it is still exactly-once: a redelivered webhook after the late payment is a no-op
    assert await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00}) is None
    assert len(w.replies(STAFF)) == staff_before + 1


# ── 2. data ───────────────────────────────────────────────────────────
async def test_a_database_failure_while_claiming_a_message_is_not_mislabelled_a_duplicate(re_world):
    w = re_world
    real = w.db.check_unique

    def flaky(table, rows, candidate):
        if table == "messages":
            raise RuntimeError("connection reset by peer")
        return real(table, rows, candidate)
    w.db.check_unique = flaky
    with pytest.raises(RuntimeError):
        await w.send("hello")                                    # the turn must FAIL LOUDLY so the failure is visible and alertable
    assert "TURN_CRASHED" in w.sink.names() and "DUPLICATE_MESSAGE_IGNORED" not in w.sink.names()


async def test_a_genuine_duplicate_delivery_is_still_ignored(re_world):
    w = re_world
    await w.send("hello", msg_id="WA-1")
    out = await w.send("hello", msg_id="WA-1")
    assert out.status == "duplicate" and "DUPLICATE_MESSAGE_IGNORED" in w.sink.names()


def test_unique_violation_detection_matches_postgres_and_supabase_shapes():
    class APIError(Exception):                                   # postgrest-py exposes the SQLSTATE as .code
        def __init__(self, code, msg): super().__init__(msg); self.code = code
    assert is_unique_violation(APIError("23505", 'duplicate key value violates unique constraint "messages_idempotency"'))
    assert is_unique_violation(Exception("{'code': '23505', 'message': 'duplicate key'}"))
    assert is_unique_violation(Exception("duplicate key value violates unique constraint on messages"))
    assert not is_unique_violation(APIError("23514", "violates check constraint messages_sender_type_check"))
    assert not is_unique_violation(RuntimeError("connection reset by peer"))


# ── 3. context ────────────────────────────────────────────────────────
async def test_a_live_nudge_is_recorded_so_the_next_turn_knows_what_was_said(re_world):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await idle_discovery(w)
    w.clock.advance(hours=25)
    assert (await nudge_tick(w))["sent"] == 1
    nudge = w.last_reply()
    rec = [m for m in w.db.rows("messages") if m["content"] == nudge]
    assert len(rec) == 1 and rec[0]["direction"] == "outbound" and rec[0]["sender_type"] == "bot"
    assert w.db.rows("conversations")[0]["last_outbound_at"]


async def test_booking_confirmation_message_is_recorded_and_staff_alerts_are_not_put_in_customer_transcripts(re_world):
    w = re_world
    await _request(w)
    n_alerts = len(w.replies(STAFF))
    await w.cap("booking").confirm(bookings(w)[0]["id"], "2026-10-03T14:00:00+01:00")
    confirm = w.last_reply()
    assert [m["sender_type"] for m in w.db.rows("messages") if m["content"] == confirm] == ["bot"]
    assert n_alerts >= 1 and not any("New inspection request" in m["content"] for m in w.db.rows("messages"))


async def test_payment_receipt_is_recorded_in_the_customers_transcript(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00})
    assert any("Payment received" in m["content"] and m["sender_type"] == "bot" for m in w.db.rows("messages"))


# ── 4. go-live ────────────────────────────────────────────────────────
async def test_a_failure_while_persisting_a_new_config_leaves_the_previous_one_serving(food_world):
    w = food_world

    def boom(p): raise RuntimeError("db down")
    w.db.rpc_activate_config_tx = boom
    with pytest.raises(RuntimeError):
        await activate(w.pdb, w.llm, w.tid, food_config(), configs=w.configs)
    assert [r["status"] for r in w.db.rows("tenant_configs")] == ["active"]
    assert await w.configs.load_active(w.tid) is not None


async def test_activation_stamps_activated_at_and_archives_exactly_the_old_version(food_world):
    w = food_world
    res = await activate(w.pdb, w.llm, w.tid, food_config(), configs=w.configs)
    rows = sorted(w.db.rows("tenant_configs"), key=lambda r: r["version"])
    assert res.ok and [r["status"] for r in rows] == ["archived", "active"] and rows[1]["activated_at"] and rows[1]["version"] == 2


# ── 5. time ───────────────────────────────────────────────────────────
async def test_a_naive_confirmation_time_is_read_in_the_tenants_timezone_not_utc(re_world):
    w = re_world
    await _request(w)
    await w.cap("booking").confirm(bookings(w)[0]["id"], "2026-10-03T14:00:00")           # no offset: staff typed local time
    stored = datetime.fromisoformat(bookings(w)[0]["confirmed_at"])
    assert stored.utcoffset() == timedelta(hours=1)                                          # Africa/Lagos
    assert "Saturday 03 October at 02:00PM" in w.last_reply()


# ── 6. latency ────────────────────────────────────────────────────────
class _P:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, 0
    async def generate(self, **_):
        self.calls += 1
        if self.fail: raise RuntimeError(f"{self.name} 503")
        return '{"reply": "hi", "actions": []}'
    async def embed(self, text, dims):
        self.calls += 1
        if self.fail: raise RuntimeError(f"{self.name} 503")
        return [0.0] * dims


async def test_a_failing_provider_is_skipped_for_a_cooldown_instead_of_retried_every_turn():
    now = [1000.0]
    a, b = _P("gemini", fail=True), _P("groq")
    gw = LLMGateway([a, b], timeout=5, provider_cooldown=30, now=lambda: now[0])
    for _ in range(3):
        _, who = await gw.generate("t", system="s", prompt="p")
        assert who == "groq"
    assert a.calls == 1                                       # tried once, then skipped: turns no longer pay its timeout
    now[0] += 31                                              # cooldown over -> given another chance
    a.fail = False
    _, who = await gw.generate("t", system="s", prompt="p")
    assert who == "gemini" and a.calls == 2


async def test_when_every_provider_is_cooling_down_we_still_try_them_rather_than_fail_closed():
    now = [0.0]
    a, b = _P("gemini", fail=True), _P("groq", fail=True)
    gw = LLMGateway([a, b], timeout=5, provider_cooldown=30, now=lambda: now[0])
    with pytest.raises(LLMUnavailable):
        await gw.generate("t", system="s", prompt="p")
    a.fail = b.fail = False                                   # both recover inside the cooldown window
    _, who = await gw.generate("t", system="s", prompt="p")
    assert who == "gemini"


async def test_embedding_failures_have_their_own_breaker_and_do_not_block_text_generation():
    now = [0.0]
    class EmbedDown(_P):
        async def embed(self, text, dims): self.calls += 1; raise RuntimeError("embed quota")
    g = EmbedDown("gemini")
    gw = LLMGateway([g], timeout=5, provider_cooldown=30, now=lambda: now[0])
    for _ in range(2):
        with pytest.raises(LLMUnavailable): await gw.embed("t", "x", fail_fast=True)
    assert g.calls == 1                                       # second embed failed fast, no second network wait
    _, who = await gw.generate("t", system="s", prompt="p")
    assert who == "gemini"                                    # text generation was never penalised for the embedding outage


# ── 7. cron ───────────────────────────────────────────────────────────
async def test_overlapping_ticks_do_not_run_concurrently_and_the_lock_is_released_after(food_world):
    w = food_world
    orig = w.configs.load_active

    async def slow(tid):
        await asyncio.sleep(0.05)
        return await orig(tid)
    w.configs.load_active = slow
    kw = dict(pdb=w.pdb, configs=w.configs, adapters=FakeAdapterFactory(w.adapter), events=w.events, llm=w.llm, clock=w.clock, extras_for=lambda tid: {})
    a, b = await asyncio.gather(run_tick(**kw), run_tick(**kw))
    assert sorted([a["status"], b["status"]]) == ["already_running", "ok"]
    assert (await run_tick(**kw))["status"] == "ok"           # released: the next cron call runs normally


# ── vault key forensics ───────────────────────────────────────────────
def test_decrypts_identifies_which_master_key_encrypted_a_credential_without_revealing_it():
    from hooze.security import Vault, decrypts
    k1, k2 = Vault.generate_key(), Vault.generate_key()
    token = Vault(k1).encrypt("evolution-api-key")
    assert decrypts(k1, token) and not decrypts(k2, token) and not decrypts("not-a-fernet-key", token) and not decrypts(k1, "garbage")


# ── 8. found in the first real WhatsApp conversation (2026-10-14 logs) ─────
from hooze.capabilities.knowledge import render_card


async def _with_cap(w, cap):
    """Set the stuck limit on the live compiled config the pipeline reads."""
    from hooze.config_engine.activation import activate
    from tests.conftest import realestate_config
    bc = realestate_config(); bc.stuck_after_user_messages = cap
    res = await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)
    assert res.ok


async def test_a_customer_who_keeps_making_progress_is_never_called_stuck_however_long_the_chat(re_world):
    """Real logs: every turn escalated to a human (slots going 3 -> 2 -> 1 missing) because the LIFETIME message count passed the limit."""
    w = re_world
    await _with_cap(w, 2)
    for slot, val in (("budget", "80m"), ("location", "Lekki"), ("purpose", "residence")):
        w.llm.say("Noted!", ("record_slots", {"slots": {slot: val}}))
        await w.send(f"my {slot} is {val}")
    assert "ESCALATED" not in w.sink.names() and w.db.rows("contacts")[0]["ai_paused"] is not True


async def test_a_customer_who_stalls_after_some_progress_is_still_escalated(re_world):
    w = re_world
    await _with_cap(w, 2)
    w.llm.say("Noted!", ("record_slots", {"slots": {"budget": "80m"}}))
    await w.send("budget is 80m")
    for i in range(2):
        w.llm.say(f"Tell me more ({i})")
        await w.send(f"hmm {i}")
    esc = w.sink.find("ESCALATED")[0]["metadata"]
    assert esc["reason"] == "QUALIFICATION_STUCK" and set(esc["missing"]) == {"location", "purpose"}


async def test_a_long_existing_conversation_gets_a_fresh_allowance_instead_of_instant_escalation(re_world):
    """A contact that existed before this fix (huge message count, no progress marker) must not be escalated on its very next message."""
    w = re_world
    await _with_cap(w, 6)
    w.llm.say("hello"); await w.send("hi")
    contact = w.db.rows("contacts")[0]
    contact["attributes"] = {k: v for k, v in (contact.get("attributes") or {}).items() if k != "qual_progress"}
    w.db.rows("conversations")[0]["user_message_count"] = 30
    w.llm.say("sure"); await w.send("tell me more")
    assert "ESCALATED" not in w.sink.names()


def test_sharing_an_faq_record_sends_the_answer_not_just_the_question():
    faq = {"title": "How do I get started?", "content": "Book a free consultation here and we scope your WhatsApp automation within 48 hours.", "data": {}}
    card = render_card(faq, [{"field": "title", "fmt": "*{v}*"}])
    assert "How do I get started?" in card and "free consultation" in card


def test_a_property_card_with_real_fields_is_unchanged_and_does_not_dump_the_description():
    prop = {"title": "Maitama Terrace", "content": "A very long marketing description that must not be repeated.", "data": {"price": "₦120m"}}
    card = render_card(prop, [{"field": "title", "fmt": "*{v}*"}, {"field": "price", "fmt": "Price: {v}"}])
    assert card == "*Maitama Terrace*\nPrice: ₦120m"

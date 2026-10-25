"""Knowledge / booking / nudges. Each test cites the pd behaviour or incident it protects."""
import re
from datetime import datetime, timezone

import pytest

from hooze.capabilities.knowledge import KnowledgeDoc, render_card, sync_documents
from tests.conftest import CUSTOMER, STAFF, make_world, realestate_config
from tests.fakes import bow_embed

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def bookings(w, **kw): return w.db.rows("bookings", **kw)


# ══ knowledge ═════════════════════════════════════════════════════════
async def test_activation_seeded_embedded_knowledge_including_media_types(re_world):
    w = re_world
    items = w.db.rows("knowledge_items")
    assert len(items) == 4 and all(i["embedding"] and i["is_active"] for i in items)          # 3 offerings + 1 FAQ
    maitama = next(i for i in items if i["title"] == "Maitama Terrace")
    assert {m["type"] for m in maitama["media"]} == {"image", "video"} and maitama["data"]["price"] == "₦150,000,000"


async def test_retrieval_puts_the_relevant_record_in_context_by_short_ref_never_uuid(re_world):
    w = re_world
    await w.send("do you have the Jabi Lakeside Flat 2 bedroom serviced")
    system = w.llm.calls[0]["system"]
    assert re.search(r"\[K\d\] Jabi Lakeside Flat", system) and not UUID_RE.search(system) and not UUID_RE.search(w.llm.calls[0]["prompt"])


async def test_nothing_relevant_tells_the_model_not_to_invent(re_world):
    w = re_world
    await w.send("quantum chromodynamics zzz")
    assert "(none matched)" in w.llm.calls[0]["system"] and "Do not invent" in w.llm.calls[0]["system"]


async def test_embedding_outage_degrades_safely(re_world):
    w = re_world
    w.llm.embed_fail = True
    out = await w.send("tell me about Jabi")
    assert out.status == "replied" and "lookup unavailable" in w.llm.calls[0]["system"] and "KNOWLEDGE_LOOKUP_FAILED" in w.sink.names()


async def test_share_item_renders_the_card_from_the_verified_record_and_sends_media(re_world):
    """pd hallucinated listing details; now the LLM only picks a ref, the backend supplies every fact."""
    w = re_world
    w.llm.say("Here's a lovely one, only ₦5,000!", ("share_item", {"ref": "K1"}))
    await w.send("Jabi Lakeside Flat 2 bedroom serviced")
    card = next(t for t in w.replies() if "Jabi Lakeside Flat" in t)
    assert card.splitlines()[0] == "*Jabi Lakeside Flat*" and "📍 Jabi" in card and "💰 ₦80,000,000" in card and "Bedrooms: 2" in card
    assert w.adapter.media == [(CUSTOMER, "https://cdn.test/jabi1.jpg", "image")]
    assert w.replies().index(card) < len(w.replies()) - 1                        # card first, then the model's chat line
    assert w.db.rows("conversations")[0]["state"] == "PRESENTING"


async def test_hallucinated_ref_sends_nothing(re_world):
    w = re_world
    w.llm.say("Sending it now!", ("share_item", {"ref": "K9"}))
    await w.send("Jabi flat")
    assert w.adapter.media == [] and not any(t.startswith("*") for t in w.replies())


@pytest.mark.parametrize("claim", ["I've sent you the details and photos.", "Here are the details of the flat."])
async def test_unearned_sent_claim_is_removed(re_world, claim):
    """pd _reply_claims_listing_sent: 'I've sent the photos' when nothing was sent."""
    w = re_world
    w.llm.say(f"Great taste. {claim}")
    await w.send("Jabi flat")
    reply = w.last_reply()
    assert "details" not in reply.lower() or "which one" in reply.lower()
    assert "Great taste." in reply


async def test_sent_claim_is_kept_when_the_item_really_was_shared(re_world):
    w = re_world
    w.llm.say("I've sent you the details and photos.", ("share_item", {"ref": "K1"}))
    await w.send("Jabi Lakeside Flat 2 bedroom serviced")
    assert "I've sent you the details" in w.last_reply()


def test_render_card_skips_missing_fields_and_joins_lists():
    item = {"title": "T", "data": {"price": "₦1", "tags": ["a", "b"], "empty": ""}}
    fields = [{"field": "title", "fmt": "*{v}*"}, {"field": "price", "fmt": "💰 {v}"}, {"field": "tags", "fmt": "{v}"}, {"field": "empty", "fmt": "X {v}"}, {"field": "nope", "fmt": "Y {v}"}]
    assert render_card(item, fields) == "*T*\n💰 ₦1\na, b"


async def test_sync_is_hash_diffed_bounded_and_deactivates_missing_docs(re_world):
    """pd audit H9 (sequential embedding froze the app) + firebase_sync content-hash diff."""
    w = re_world
    calls = []
    async def embedder(t): calls.append(t); return bow_embed(t)
    docs = [KnowledgeDoc(f"x{i}", f"Doc {i}", f"content {i}") for i in range(5)]
    assert (await sync_documents(w.tdb, embedder, docs, source="t"))["created"] == 5 and len(calls) == 5
    assert (await sync_documents(w.tdb, embedder, docs, source="t"))["unchanged"] == 5 and len(calls) == 5      # no re-embedding
    docs[2].content = "changed"
    assert (await sync_documents(w.tdb, embedder, docs, source="t"))["updated"] == 1 and len(calls) == 6
    stats = await sync_documents(w.tdb, embedder, docs[:3], source="t")
    assert stats["deactivated"] == 2 and sum(1 for r in w.db.rows("knowledge_items", source="t") if r["is_active"]) == 3
    assert not any(r["source"] == "t" for r in w.db.rows("knowledge_items") if r["title"] == "Maitama Terrace")   # other sources untouched


async def test_sync_embedding_failure_is_counted_and_never_writes_a_half_record(re_world):
    w = re_world
    async def boom(t): raise RuntimeError("quota")
    stats = await sync_documents(w.tdb, boom, [KnowledgeDoc("a", "A", "a")], source="t2")
    assert stats["failed"] == 1 and w.db.rows("knowledge_items", source="t2") == []


# ══ booking ═══════════════════════════════════════════════════════════
async def _request(w, subject="Maitama Terrace", when="Saturday 2pm"):
    w.llm.say("Booking that!", ("request_booking", {"subject": subject, "preferred_time": when}))
    return await w.send(f"I'd like to see {subject} {when}")


async def test_booking_request_notifies_staff_keeps_customer_words_and_moves_state(re_world):
    w = re_world
    await _request(w)
    (b,) = bookings(w)
    assert b["status"] == "pending" and b["preferred_text"] == "Saturday 2pm" and b["suggested_at"]      # a GUESS, still pending
    assert "sent your inspection request" in w.last_reply() and "Booking that!" not in w.last_reply()
    alert = w.last_reply(STAFF)
    assert "New inspection request" in alert and "Saturday 2pm" in alert and "not booked yet" in alert and CUSTOMER in alert
    assert w.db.rows("conversations")[0]["state"] == "BOOKED"


async def test_unparseable_time_has_no_guess_and_no_fake_precision(re_world):
    w = re_world
    await _request(w, when="whenever you're free")
    assert bookings(w)[0]["suggested_at"] is None and "Best guess" not in w.last_reply(STAFF)


async def test_human_confirms_via_atomic_rpc_customer_is_told_and_transcript_records_it(re_world):
    w = re_world
    await _request(w)
    bid = bookings(w)[0]["id"]
    row = await w.cap("booking").confirm(bid, "2026-10-03T14:00:00+01:00", "gate code 4411")
    assert row and bookings(w)[0]["status"] == "confirmed" and bookings(w)[0]["staff_notes"] == "gate code 4411"
    assert "Confirmed" in w.last_reply() and "Saturday 03 October at 02:00PM" in w.last_reply()
    assert any(m["content"].startswith("Booking confirmed") for m in w.db.rows("messages") if m["sender_type"] == "system")


async def test_double_confirm_and_confirm_then_decline_are_harmless_noops(re_world):
    w = re_world
    await _request(w)
    bid, cap = bookings(w)[0]["id"], w.cap("booking")
    await cap.confirm(bid, "2026-10-03T14:00:00+01:00")
    n = len(w.replies())
    assert await cap.confirm(bid, "2026-10-04T10:00:00+01:00") is None and await cap.decline(bid) is None
    assert len(w.replies()) == n and bookings(w)[0]["status"] == "confirmed"


async def test_invalid_confirm_time_is_rejected_before_touching_the_database(re_world):
    w = re_world
    await _request(w)
    with pytest.raises(ValueError):
        await w.cap("booking").confirm(bookings(w)[0]["id"], "next tuesday-ish")
    assert bookings(w)[0]["status"] == "pending"


async def test_decline_tells_the_customer_and_invites_another_slot(re_world):
    w = re_world
    await _request(w)
    await w.cap("booking").decline(bookings(w)[0]["id"], "fully booked")
    assert bookings(w)[0]["status"] == "declined" and "another day" in w.last_reply()


# ══ nudges ════════════════════════════════════════════════════════════
def set_nudge_cfg(w, **over):
    w.db.rows("tenant_configs")[0]["compiled"]["capabilities"]["nudges"].update(over)


async def idle_discovery(w):
    """A contact who said hello and went quiet, in DISCOVERY (idle_after_hours=24)."""
    await w.send("hi")
    assert w.db.rows("conversations")[0]["state"] == "DISCOVERY"


def nstate(w): return w.db.rows("nudge_state")[0]


async def tick(w): return await (await _fresh(w)).tick()
async def _fresh(w):
    return w.cap("nudges", await w.configs.load_active(w.tid))


async def test_dry_run_is_the_default_it_sends_nothing_logs_and_does_not_advance_the_sequence(re_world):
    w = re_world
    await idle_discovery(w)
    n = len(w.replies())
    w.clock.advance(hours=25)
    r = await tick(w)
    assert r["dry_run"] == 1 and r["sent"] == 0 and len(w.replies()) == n
    assert nstate(w)["step"] == 0 and "Hi Ada" in w.sink.find("NUDGE_DRY_RUN_WOULD_SEND")[0]["metadata"]["preview"]
    assert (await tick(w))["dry_run"] == 0                                    # the cooldown stamp stops it logging the same preview every tick


async def test_going_live_after_a_dry_run_starts_from_the_first_message_without_any_reset(re_world):
    w = re_world
    await idle_discovery(w)
    w.clock.advance(hours=25); await tick(w); w.clock.advance(hours=25); await tick(w); w.clock.advance(hours=25); await tick(w)   # three dry cycles
    set_nudge_cfg(w, dry_run=False)
    w.clock.advance(hours=25)
    r = await tick(w)
    assert r["sent"] == 1 and nstate(w)["step"] == 1
    assert "Hi Ada" in w.last_reply()                                           # the FIRST template, not the third


async def test_live_nudge_sends_records_and_respects_cooldown_then_exhausts(re_world):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await idle_discovery(w)
    w.clock.advance(hours=25)
    assert (await tick(w))["sent"] == 1 and "just checking in" in w.last_reply() and nstate(w)["step"] == 1
    assert w.db.rows("messages")[-1]["sender_type"] == "bot"                                    # recorded in the transcript
    n = len(w.replies())
    assert (await tick(w))["sent"] == 0 and len(w.replies()) == n                              # cooldown: no second send in the same window
    for expected_step in (2, 3):
        w.clock.advance(hours=25); assert (await tick(w))["sent"] == 1 and nstate(w)["step"] == expected_step
    w.clock.advance(hours=25)
    assert (await tick(w))["exhausted"] == 1 and nstate(w)["status"] == "EXHAUSTED"
    n = len(w.replies()); w.clock.advance(hours=25); await tick(w)
    assert len(w.replies()) == n                                                                # bounded sequence


async def test_reserve_before_send_a_failed_send_never_causes_a_resend_loop(re_world):
    """pd 2026-08-13: one lead received ~20 follow-ups because bookkeeping ran after (and retried with) the send."""
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await idle_discovery(w)
    w.clock.advance(hours=25)
    w.adapter.fail_sends, w.adapter.attempts = True, 0
    assert (await tick(w))["skipped"] == 1 and w.adapter.attempts >= 1
    assert nstate(w)["last_nudge_at"] is not None and nstate(w)["step"] == 0                    # reserved BEFORE sending
    attempts = w.adapter.attempts
    for _ in range(3):
        await tick(w)
    assert w.adapter.attempts == attempts                                                       # cooldown blocks every retry cycle


async def test_nothing_is_sent_outside_the_tenants_local_sending_window(re_world):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await idle_discovery(w)
    w.clock.advance(hours=34)                                                                   # 21:00 UTC = 22:00 Lagos
    assert (await tick(w))["sent"] == 0
    w.clock.advance(hours=11)                                                                   # 09:00 Lagos next day
    assert (await tick(w))["sent"] == 1


@pytest.mark.parametrize("phrase,status", [("please stop messaging me", "OPTED_OUT"), ("I already bought one elsewhere", "LOST")])
async def test_opt_out_and_lost_phrases_stop_nudges_immediately(re_world, phrase, status):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await w.send(phrase)
    assert nstate(w)["status"] == status
    w.clock.advance(hours=100)
    assert (await tick(w))["sent"] == 0


async def test_customer_reply_resets_the_sequence(re_world):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await idle_discovery(w)
    w.clock.advance(hours=25); await tick(w)
    assert nstate(w)["step"] == 1 and nstate(w)["status"] == "IDLE"
    await w.send("sorry, still interested")
    assert nstate(w)["step"] == 0 and nstate(w)["status"] == "ACTIVE"


async def test_paused_contacts_and_kill_switch_are_respected(re_world, monkeypatch):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    await idle_discovery(w)
    w.clock.advance(hours=25)
    w.db.rows("contacts")[0]["ai_paused"] = True                                                # a human owns this chat
    assert (await tick(w))["sent"] == 0
    w.db.rows("contacts")[0]["ai_paused"] = False
    attempts = w.adapter.attempts                                                               # (includes the initial "hi" reply)
    from hooze.settings import Settings
    import hooze.capabilities.nudges as nz
    monkeypatch.setattr(nz, "get_settings", lambda: Settings(_env_file=None, nudges_globally_enabled=False))
    assert (await tick(w))["status"] == "disabled" and w.adapter.attempts == attempts


async def test_unanswered_booking_invite_escalates_to_a_human_instead_of_nagging(re_world):
    w = re_world
    set_nudge_cfg(w, dry_run=False)
    w.llm.say("Let's set up a time!", intent="booking")
    await w.send("I want to see it")
    assert w.db.rows("conversations")[0]["state"] == "BOOKING"
    w.clock.advance(hours=21); assert (await tick(w))["sent"] == 1 and "what day works" in w.last_reply()     # 09:00 Lagos: first in-window moment after 12h idle
    w.clock.advance(hours=24)
    r = await tick(w)
    assert r["escalated"] == 1 and r["sent"] == 0 and "hasn't replied" in w.last_reply(STAFF) and nstate(w)["status"] == "EXHAUSTED"

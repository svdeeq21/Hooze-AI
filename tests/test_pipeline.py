"""Pipeline behaviour common to every tenant, plus handoff and qualification (audit C4/M6/M7/H3 regressions)."""
import pytest

from hooze.capabilities.qualification import coerce_slot, normalize_money
from tests.conftest import CUSTOMER, STAFF, food_config, make_world, realestate_config
from tests.fakes import inbound


# ── idempotency / robustness ───────────────────────────────────────────
async def test_duplicate_provider_message_is_processed_once(re_world):
    w = re_world
    w.llm.say("Hi there!")
    first = await w.send("hello", msg_id="dup-1")
    second = await w.send("hello", msg_id="dup-1")                      # provider redelivery
    assert (first.status, second.status) == ("replied", "duplicate")
    assert len(w.llm.calls) == 1 and len(w.replies()) == 1
    assert "DUPLICATE_MESSAGE_IGNORED" in w.sink.names()


async def test_hallucinated_tool_and_bad_args_are_logged_noops_never_writes(re_world):
    w = re_world
    w.llm.say("Done!", ("delete_all_customers", {}), ("record_slots", {"slots": "not-a-dict"}))
    out = await w.send("hi")
    assert out.status == "replied"
    assert "LLM_UNKNOWN_TOOL" in w.sink.names() and "LLM_BAD_TOOL_ARGS" in w.sink.names()
    attrs = w.db.rows("contacts")[0]["attributes"]
    assert not attrs.get("slots") and set(attrs) <= {"qual_progress"}          # no customer data written (only the stall-tracking marker may exist)


@pytest.mark.parametrize("bad", ['```json\n{"a": 1}\n```', '{"actions": [], "reply": "x"', '[{"function": "record_slots"}]', '{"function": "record_slots", "slots": {}}'])
async def test_reply_that_looks_like_leaked_json_or_code_is_never_sent(re_world, bad):
    w = re_world
    w.llm.say(bad)
    await w.send("hi")
    sent = w.last_reply()
    assert sent.startswith("Sorry — could you say that again?") and "REPLY_LEAK_BLOCKED" in w.sink.names()


async def test_reply_containing_an_internal_uuid_is_blocked(re_world):
    w = re_world
    w.llm.say("Sure, ref 3f2b8c1e-9a4d-4e7b-8c11-2d5f6a7b8c9d is yours")
    await w.send("hi")
    assert "3f2b8c1e" not in w.last_reply()


async def test_llm_outage_hands_off_to_a_human_instead_of_going_silent(re_world):
    w = re_world
    w.llm.unavailable = True
    out = await w.send("I want to buy a house")
    assert out.status == "handoff"
    assert "team will pick this up" in w.last_reply() or "member of the team" in w.last_reply()
    assert any("LLM_ALL_PROVIDERS_FAILED" in m for m in w.replies(STAFF)) or any("Human needed" in m for m in w.replies(STAFF))
    assert w.db.rows("contacts")[0]["ai_paused"] is True


async def test_media_gets_canned_reply_without_calling_the_llm(re_world):
    w = re_world
    for kind in ("image", "document", "location"):
        out = await w.send("", kind=kind)
        assert out.status == "media"
    assert w.llm.calls == []
    assert "can't view images" in w.replies()[0]
    n = len(w.replies())
    await w.send("", kind="sticker")                                     # stickers/reactions: silent
    assert len(w.replies()) == n


async def test_voice_note_is_transcribed_and_flows_through_the_normal_pipeline(re_world):
    w = re_world
    async def transcriber(audio: bytes): return "I want a 3 bedroom in Maitama"
    w.pipeline.transcriber = transcriber
    w.llm.say("Lovely — Maitama it is.")
    out = await w.send("", kind="audio")
    assert out.status == "replied"
    assert "3 bedroom in Maitama" in w.llm.calls[0]["prompt"]


async def test_untranscribable_voice_note_asks_for_text(re_world):
    w = re_world
    async def transcriber(audio): return None
    w.pipeline.transcriber = transcriber
    out = await w.send("", kind="audio")
    assert out.status == "media" and "type that" in w.last_reply()


async def test_group_and_own_messages_are_ignored(re_world):
    w = re_world
    m = inbound("hi"); m.is_group = True
    assert (await w.pipeline.handle_inbound(w.channel, m)).status == "ignored"
    m = inbound("hi"); m.from_me = True
    assert (await w.pipeline.handle_inbound(w.channel, m)).status == "ignored"
    assert w.llm.calls == []


async def test_first_contact_welcome_is_deterministic_and_sent_once():
    bc = realestate_config(); bc.communication.greeting = "Welcome to Praise Dynasty! 🏡"
    w = await make_world(bc)
    w.llm.say("What kind of property are you after?")
    await w.send("hi")
    w.llm.say("Noted.")
    await w.send("something in Jabi")
    assert w.replies().count("Welcome to Praise Dynasty! 🏡") == 1


async def test_rate_limit_stops_runaway_replies():
    bc = realestate_config(); bc.stuck_after_user_messages = 999           # keep the stuck-lead rule out of this test
    w = await make_world(bc)
    for i in range(8):
        await w.send(f"msg {i}")
    out = await w.send("one more")
    assert out.status == "gated" and out.detail == "rate_limited" and "RATE_LIMITED" in w.sink.names()


async def test_send_failure_does_not_record_a_phantom_outbound_message(re_world):
    w = re_world
    w.adapter.fail_sends = True
    w.llm.say("Hello!")
    with pytest.raises(RuntimeError):
        await w.send("hi")
    assert [m for m in w.db.rows("messages") if m["sender_type"] == "bot"] == []    # H3: bookkeeping only after a confirmed send


# ── multi-tenancy end-to-end ───────────────────────────────────────────
async def test_same_phone_at_two_tenants_is_two_isolated_customers():
    a = await make_world(realestate_config(), tenant_name="acme")
    b = await make_world(food_config(), tenant_name="bolt", db=a.db, clock=a.clock)   # SAME database
    a.llm.say("Hi from acme"); b.llm.say("Hi from bolt")
    await a.send("hello", phone=CUSTOMER); await b.send("hello", phone=CUSTOMER)
    contacts = a.db.rows("contacts")
    assert len(contacts) == 2 and {c["tenant_id"] for c in contacts} == {a.tid, b.tid}
    assert {c["id"] for c in contacts}.isdisjoint({x["contact_id"] for x in a.db.rows("conversations") if x["tenant_id"] != x["tenant_id"]})
    a_msgs = {m["content"] for m in a.db.rows("messages", tenant_id=a.tid)}
    assert "Hi from bolt" not in a_msgs
    a_ctx = await a.configs.load_active(a.tid); b_ctx = await b.configs.load_active(b.tid)
    assert a_ctx.persona.business_name == "Praise Dynasty Properties" and b_ctx.persona.business_name == "Savoury Spud"


# ── staff routing ──────────────────────────────────────────────────────
async def test_staff_messages_never_reach_the_llm(food_world):
    w = food_world
    out = await w.send("status", phone=STAFF)
    assert out.status == "staff" and w.llm.calls == [] and "Accepting orders" in w.last_reply(STAFF)
    out = await w.send("blah blah", phone=STAFF)
    assert "recognise" in w.last_reply(STAFF)
    assert w.db.rows("contacts") == []                                    # staff are not customers


# ── prompt & playbook wiring ───────────────────────────────────────────
async def test_system_prompt_carries_persona_rules_facts_state_goal_and_tools(re_world):
    w = re_world
    await w.send("hi")
    sys_prompt = w.llm.calls[0]["system"]
    for needle in ("Praise Dynasty Properties", "NEVER say or imply: guaranteed returns", "https://praisedynasty.example/brochure.pdf",
                   "CURRENT CONVERSATION STAGE: NEW", "record_slots", "share_item", "request_booking", "request_human"):
        assert needle in sys_prompt, needle
    assert "add_product" not in sys_prompt                                # ordering tools are absent for a consult tenant


async def test_playbook_advances_new_to_discovery_and_records_transition(re_world):
    w = re_world
    await w.send("hi")
    assert w.db.rows("conversations")[0]["state"] == "DISCOVERY"
    ev = w.sink.find("STATE_TRANSITION")[0]["metadata"]
    assert (ev["from_state"], ev["to_state"]) == ("NEW", "DISCOVERY")


async def test_turn_completed_event_carries_per_stage_timings(re_world):
    w = re_world
    await w.send("hi")
    t = w.sink.find("TURN_COMPLETED")[0]["metadata"]["timings"]
    assert {"context_ms", "llm_ms", "tools_ms", "send_ms", "total_ms"} <= set(t)


async def test_rolling_summary_folds_only_aged_out_messages():
    bc = realestate_config(); bc.stuck_after_user_messages = 999
    w = await make_world(bc, summary_interval=2)
    w.db.rows("tenant_configs")[0]["compiled"]["history_window"] = 4       # tune the stored compiled config (the loader re-reads it each turn)
    for i in range(6):
        w.llm.say(f"reply {i}")
        await w.send(f"question {i}")
    conv = w.db.rows("conversations")[0]
    assert conv["summary"].startswith("SUMMARY#") and conv["summary_upto"] > 0
    assert "question 0" in w.llm.summaries[0] and "question 5" not in w.llm.summaries[0]   # recent turns are NOT folded
    assert "SUMMARY#" in w.llm.calls[-1]["prompt"]                                          # and the summary is what the LLM sees


# ── handoff ────────────────────────────────────────────────────────────
async def test_explicit_human_request_pauses_ai_alerts_staff_and_skips_the_llm(re_world):
    w = re_world
    out = await w.send("I want to speak to a human please")
    assert out.status == "gated" and w.llm.calls == []
    assert "Sadiq" in w.last_reply()                                       # contact_name is config, not hard-coded (audit M7)
    alert = w.replies(STAFF)[-1]
    assert "Human needed" in alert and CUSTOMER in alert and f"RESUME {CUSTOMER}" in alert
    assert w.db.rows("contacts")[0]["ai_paused"] is True
    assert w.db.rows("conversations")[0]["status"] == "handoff"
    n = len(w.replies())
    out = await w.send("hello??")                                          # while a human owns it
    assert out.status == "paused" and len(w.replies()) == n and w.llm.calls == []


@pytest.mark.parametrize("text", ["you can call me John", "just call me Ade", "call me Chinedu", "Please call me Mrs Okafor"])
async def test_call_me_name_introductions_are_not_human_requests(re_world, text):
    """audit M6: 'you can call me John' used to escalate a happy lead to a human."""
    w = re_world
    w.llm.say("Nice to meet you!")
    out = await w.send(text)
    assert out.status == "replied" and w.db.rows("contacts")[0]["ai_paused"] is False


@pytest.mark.parametrize("text", ["please call me back", "call me", "give me a call tomorrow", "can I talk to someone"])
async def test_genuine_call_requests_do_escalate(re_world, text):
    out = await re_world.send(text)
    assert out.status == "gated" and re_world.db.rows("contacts")[0]["ai_paused"] is True


async def test_staff_can_resume_a_paused_conversation(re_world):
    w = re_world
    await w.send("I need a real person")
    assert w.db.rows("contacts")[0]["ai_paused"]
    out = await w.send(f"resume {CUSTOMER}", phone=STAFF)
    assert out.status == "staff" and "Resumed" in w.last_reply(STAFF)
    w.llm.say("Welcome back!")
    assert (await w.send("hi again")).status == "replied"


async def test_model_can_request_human_via_tool_and_customer_hears_the_authoritative_message(re_world):
    w = re_world
    w.llm.say("Sure, one sec!", ("request_human", {"reason": "customer upset"}))
    await w.send("this is unacceptable")
    assert "Sadiq" in w.last_reply() and w.db.rows("contacts")[0]["ai_paused"]


# ── qualification (audit C4) ───────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [("₦150,000,000", 150_000_000), ("150m", 150_000_000), ("150 million", 150_000_000), ("1.5bn", 1_500_000_000),
                                           ("N150M", 150_000_000), ("2 bedroom flat for 50m", 50_000_000), ("500k", 500_000), ("5 months", None), ("hello", None)])
def test_money_normalisation_regressions(text, expected):
    assert normalize_money(text) == expected


def test_coerce_slot_types():
    enum = {"type": "enum", "values": ["living", "investment"], "aliases": {"living": ["live in it"]}}
    assert coerce_slot(enum, "INVESTMENT") == (True, "investment") and coerce_slot(enum, "live in it") == (True, "living")
    assert coerce_slot(enum, "vibes")[0] is False
    assert coerce_slot({"type": "bool"}, "Yes") == (True, True) and coerce_slot({"type": "bool"}, "maybe")[0] is False
    assert coerce_slot({"type": "number"}, "3 bedrooms") == (True, 3.0)
    assert coerce_slot({"type": "text"}, "  Jabi ") == (True, "Jabi") and coerce_slot({"type": "text"}, "  ")[0] is False


async def test_record_slots_validates_normalises_and_completes_qualification(re_world):
    w = re_world
    w.llm.say("Got it — anything else?", ("record_slots", {"slots": {"budget": "₦150,000,000", "location": "Jabi", "purpose": "vibes", "colour": "blue"}}))
    await w.send("I have ₦150,000,000 and want Jabi")
    slots = w.db.rows("contacts")[0]["attributes"]["slots"]
    assert slots == {"budget": 150_000_000, "location": "Jabi"}            # invalid enum + unknown slot rejected, money normalised
    w.llm.say("Perfect.", ("record_slots", {"slots": {"purpose": "living"}}))
    await w.send("to live in")
    assert "QUALIFICATION_COMPLETE" in w.sink.names()


async def test_qualification_context_lists_known_and_missing_and_forbids_reasking(re_world):
    w = re_world
    w.llm.say("ok", ("record_slots", {"slots": {"budget": "80m"}}))
    await w.send("budget is 80m")
    await w.send("what do you have?")
    prompt_system = w.llm.calls[-1]["system"]
    assert "budget: 80000000" in prompt_system and "STILL NEEDED" in prompt_system and "never re-ask" in prompt_system


async def test_stuck_lead_is_escalated_to_a_human_instead_of_rotting_silently(re_world):
    """audit C4: leads sat in a pre-search stage for 500+ messages. Now: after N messages with required slots missing, a human is told."""
    w = re_world                                                            # stuck_after_user_messages=6 in the fixture config
    for i in range(6):
        w.llm.say(f"Tell me more ({i})")
        await w.send(f"hmm {i}")
    assert w.db.rows("contacts")[0]["ai_paused"] is True
    assert any("QUALIFICATION_STUCK" in m for m in w.replies(STAFF))
    esc = w.sink.find("ESCALATED")[0]["metadata"]
    assert esc["reason"] == "QUALIFICATION_STUCK" and set(esc["missing"]) == {"budget", "location", "purpose"}

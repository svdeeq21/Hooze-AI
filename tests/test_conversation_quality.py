"""Fixes for what the first real WhatsApp conversations showed (chat export + server logs, 14-16 Oct 2026).

  1. double greeting       : the scripted welcome AND the model's own greeting were both sent for one "Hello"
  2. "request for X ()"    : a booking was created with no time; the template printed empty parentheses
  3. repeated brochure     : the same full card was re-sent on every pitch
  4. "could you rephrase"  : unparseable model output (not a misunderstanding) went straight to the customer
  5. AI outage             : paused the customer's assistant and left them stuck; now: one holding message + retry, human only if it persists
  6. phone formats         : 0801... (what Nigerians actually type) silently never matched
"""
import httpx
import pytest

from hooze.alerts import WhatsAppAlertSink
from hooze.config_engine.activation import activate
from hooze.kernel.errors import describe_exc
from hooze.kernel.phone import normalize_phone
from hooze.llm.gateway import LLMGateway, LLMUnavailable, OpenAICompatProvider, build_default_gateway
from hooze.runtime.inbox import Inbox
from hooze.settings import Settings
from tests.conftest import CUSTOMER, STAFF, realestate_config
from tests.fakes import FakeAdapter
from tests.test_api import HybridFactory, wa_body
from tests.test_capabilities import bookings


async def _live(w, **comm):
    bc = realestate_config()
    for k, v in comm.items():
        setattr(bc.communication, k, v)
    assert (await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)).ok


# ── 1. double greeting ────────────────────────────────────────────────
async def test_a_plain_hello_gets_the_scripted_welcome_only_not_a_second_greeting_from_the_model(re_world):
    w = re_world
    await _live(w, greeting="Welcome to Praise Dynasty. Tell me what you're looking for.")
    w.llm.say("Good morning! How can we assist your business today?", intent="greeting")
    await w.send("Hello Good Morning")
    assert w.replies() == ["Welcome to Praise Dynasty. Tell me what you're looking for."]
    assert "GREETING_REPLY_SUPPRESSED" in w.sink.names()


async def test_a_real_question_gets_the_welcome_then_the_answer_and_the_model_is_told_not_to_greet_again(re_world):
    w = re_world
    await _live(w, greeting="Welcome to Praise Dynasty.")
    w.llm.say("Our 2-bedroom flats start from ₦80m. What area do you prefer?", intent="question")
    await w.send("how much is a 2 bedroom flat?")
    assert w.replies() == ["Welcome to Praise Dynasty.", "Our 2-bedroom flats start from ₦80m. What area do you prefer?"]
    assert "ALREADY been sent" in w.llm.calls[0]["prompt"]


async def test_without_a_scripted_welcome_a_greeting_is_answered_by_the_model_as_before(re_world):
    w = re_world
    w.llm.say("Hi! How can I help?", intent="greeting")
    await w.send("hello")
    assert w.replies() == ["Hi! How can I help?"] and "ALREADY been sent" not in w.llm.calls[0]["prompt"]


async def test_the_welcome_is_sent_once_per_new_conversation_only(re_world):
    w = re_world
    await _live(w, greeting="Welcome!")
    w.llm.say("hi", intent="greeting"); await w.send("hello")
    w.llm.say("Sure, one moment.", intent="question"); await w.send("do you have flats in Lekki")
    assert w.replies() == ["Welcome!", "Sure, one moment."]


# ── 2. booking without a time ─────────────────────────────────────────
async def test_a_booking_proposed_without_a_time_asks_for_the_time_instead_of_booking(re_world):
    w = re_world
    w.llm.say("Great, I'll book that.", ("request_booking", {"subject": "General consultation", "preferred_time": ""}), intent="booking")
    await w.send("yes I would")
    assert bookings(w) == [] and w.replies() and "What day and time" in w.replies()[-1] and "()" not in w.replies()[-1]
    assert not any("New inspection request" in t for _, t in w.adapter.sent if _ == STAFF)       # no premature staff alert


@pytest.mark.parametrize("vague", ["yes", "ok", "any", "sure", " ", "n/a", "Yes, I would!", "Okay."])
async def test_non_answers_are_not_treated_as_a_time(re_world, vague):
    w = re_world
    w.llm.say("Booking it.", ("request_booking", {"subject": "General consultation", "preferred_time": vague}), intent="booking")
    await w.send("yes")
    assert bookings(w) == [] and "What day and time" in w.replies()[-1]


async def test_a_booking_with_a_real_time_is_recorded_and_the_confirmation_never_has_empty_parentheses(re_world):
    w = re_world
    w.llm.say("Booking it.", ("request_booking", {"subject": "General consultation", "preferred_time": "tomorrow 3pm"}), intent="booking")
    await w.send("tomorrow at 3pm please")
    assert len(bookings(w)) == 1 and "(tomorrow 3pm)" in w.replies()[-1] and "()" not in w.replies()[-1]


def test_the_prompt_tells_the_model_bookings_need_a_time_first():
    from hooze.runtime.prompt import PLATFORM_RULES
    assert "booking" in PLATFORM_RULES.lower() and "day and time" in PLATFORM_RULES.lower()


# ── 3. repeated brochure ──────────────────────────────────────────────
async def test_a_record_card_is_not_sent_twice_to_the_same_customer_within_a_day(re_world):
    w = re_world
    w.llm.say("Here you go!", ("share_item", {"ref": "K1"}))
    await w.send("Jabi Lakeside Flat 2 bedroom serviced")
    cards_before = [t for t in w.replies() if t.startswith("*Jabi Lakeside Flat*")]
    w.llm.say("I've sent you the details again!", ("share_item", {"ref": "K1"}))
    await w.send("Jabi Lakeside Flat 2 bedroom serviced, tell me more")
    cards_after = [t for t in w.replies() if t.startswith("*Jabi Lakeside Flat*")]
    assert len(cards_before) == 1 and len(cards_after) == 1 and len(w.adapter.media) == 1            # no second card, no second photo
    assert "again" not in w.replies()[-1].lower()                                                    # and the false "I've sent it" claim is stripped


async def test_the_card_can_be_shared_again_after_the_window(re_world):
    w = re_world
    w.llm.say("Here you go!", ("share_item", {"ref": "K1"}))
    await w.send("Jabi Lakeside Flat 2 bedroom serviced")
    w.clock.advance(hours=25)
    w.llm.say("Here it is again.", ("share_item", {"ref": "K1"}))
    await w.send("Jabi Lakeside Flat 2 bedroom serviced")
    assert len([t for t in w.replies() if t.startswith("*Jabi Lakeside Flat*")]) == 2


def test_the_model_is_told_to_share_a_card_only_when_asked_and_to_tailor_the_pitch_to_the_customers_business():
    from hooze.runtime.prompt import PLATFORM_RULES
    text = PLATFORM_RULES.lower()
    assert "never paste" in text and "own business" in text and "rephrase" in text


# ── 4. unparseable output ─────────────────────────────────────────────
class _P:
    def __init__(self, name, outputs):
        self.name, self.outputs, self.calls = name, list(outputs), 0
    async def generate(self, **kw):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception): raise out
        return out
    async def embed(self, t, d): raise NotImplementedError


GOOD = '{"reply": "Sure — what day suits you?", "actions": [], "intent": "booking"}'


async def test_unparseable_output_is_retried_on_the_other_provider_before_the_customer_sees_a_fallback():
    a, b = _P("groq", ["I would like to help! (not json)"]), _P("gemini", [GOOD])
    res = await LLMGateway([a, b], timeout=5, provider_cooldown=0).interpret("t", system="s", prompt="p")
    assert res.interpretation.reply == "Sure — what day suits you?" and res.provider == "gemini" and not res.degraded


async def test_if_every_attempt_is_unparseable_the_fallback_is_neutral_and_flagged_degraded():
    a, b = _P("groq", ["nope"]), _P("gemini", ["still nope"])
    res = await LLMGateway([a, b], timeout=5, provider_cooldown=0).interpret("t", system="s", prompt="p")
    assert res.degraded and "rephrase" not in res.interpretation.reply.lower() and "didn't catch" in res.interpretation.reply.lower()


async def test_a_single_provider_gets_one_repair_attempt_with_a_stricter_instruction():
    seen = []
    class One(_P):
        async def generate(self, **kw):
            seen.append(kw["prompt"]); return await super().generate(**kw)
    p = One("groq", ["garbage", GOOD])
    res = await LLMGateway([p], timeout=5, provider_cooldown=0).interpret("t", system="s", prompt="the prompt")
    assert res.interpretation.reply.startswith("Sure") and "ONLY the JSON" in seen[1] and seen[1].startswith("the prompt")


# ── 5. AI outage ──────────────────────────────────────────────────────
async def _inbox(w):
    from tests.test_api import container
    return container(w).inbox


async def test_an_ai_outage_sends_one_holding_message_keeps_the_assistant_on_and_answers_properly_on_retry(re_world):
    w = re_world
    w.llm.unavailable = True
    c = await _inbox(w)
    eid, _ = await c.enqueue({"tenant_id": w.tid, "id": w.channel["id"]}, wa_body("how much is a flat?", msg_id="O1"), "O1")
    assert await c.process(eid) == "retry"
    assert w.replies() == ["One moment — I'll be right with you."]
    assert w.db.rows("contacts")[0]["ai_paused"] is not True and "ESCALATED" not in w.sink.names()
    w.llm.unavailable = False
    w.llm.say("Our flats start from ₦80m. Which area?", intent="question")
    w.clock.advance(seconds=60)
    assert (await c.recover())["done"] == 1
    assert w.replies() == ["One moment — I'll be right with you.", "Our flats start from ₦80m. Which area?"]       # the holding message didn't fool the replay guard


async def test_a_persistent_outage_hands_over_to_a_human_on_the_third_attempt_only(re_world):
    w = re_world
    w.llm.unavailable = True
    c = await _inbox(w)
    eid, _ = await c.enqueue({"tenant_id": w.tid, "id": w.channel["id"]}, wa_body("hello?", msg_id="O2"), "O2")
    await c.process(eid)
    for _ in range(2):
        w.clock.advance(seconds=700)
        await c.recover()
    assert "ESCALATED" in w.sink.names() and w.db.rows("contacts")[0]["ai_paused"] is True
    assert w.replies().count("One moment — I'll be right with you.") == 1                 # never spammed the customer with holding messages
    assert any("member of the team" in r for r in w.replies())
    assert [r["status"] for r in w.db.rows("inbound_events")] == ["done"]


# ── 6. the AI chain ───────────────────────────────────────────────────
def test_the_provider_order_is_configurable_so_the_reliable_one_can_go_first():
    mk = lambda **kw: [p.name for p in build_default_gateway(Settings(_env_file=None, gemini_api_key="g", groq_api_key="q", **kw)).providers]
    assert mk() == ["gemini", "groq"]
    assert mk(llm_provider_order="groq,gemini") == ["groq", "gemini"]
    assert mk(llm_provider_order="groq") == ["groq", "gemini"]                           # a provider left out of the list is appended, never dropped
    assert mk(llm_provider_order="bogus, groq") == ["groq", "gemini"]
    assert [p.name for p in build_default_gateway(Settings(_env_file=None, groq_api_key="q", llm_provider_order="gemini,groq")).providers] == ["groq"]


async def test_groq_requests_carry_the_reasoning_effort_only_when_configured_and_empty_replies_explain_themselves():
    sent = []
    def handler(req: httpx.Request):
        import json
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
    mk = lambda effort: OpenAICompatProvider("groq", "https://x/v1", "k", "openai/gpt-oss-120b", reasoning_effort=effort, http_factory=lambda timeout=30.0: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    for effort in ("low", ""):
        with pytest.raises(ValueError) as e:
            await mk(effort).generate(system="s", prompt="p", json_mode=True, max_tokens=900, temperature=0.2)
    assert sent[0]["reasoning_effort"] == "low" and "reasoning_effort" not in sent[1]
    assert "finish_reason=length" in str(e.value) and "GROQ_REASONING_EFFORT" in str(e.value)


# ── 7. phone formats ──────────────────────────────────────────────────
@pytest.mark.parametrize("raw, want", [
    ("08012345678", "2348012345678"), ("+234 801 234 5678", "2348012345678"), ("8012345678", "2348012345678"), ("2348012345678", "2348012345678"),
    ("00234 801 234 5678", "2348012345678"), ("(0801) 234-5678", "2348012345678"), ("+44 7911 123456", "447911123456"), ("abc", ""), ("", ""),
    ("+234 (0) 801 234 5678", "2348012345678"), ("47 912 34 567", "4791234567"), ("9021112222", "2349021112222")])
def test_phone_numbers_are_normalised_to_the_international_digits_whatsapp_uses(raw, want):
    assert normalize_phone(raw) == want


def test_staff_numbers_typed_the_local_way_are_normalised_when_the_config_is_compiled():
    from hooze.config_engine.compiler import compile_config
    bc = realestate_config(); bc.handoff.staff_phones = ["08012345678", "+234 902 111 2222"]
    assert compile_config(bc, 1).runtime.staff_numbers == ["2348012345678", "2349021112222"]


def test_alert_numbers_typed_the_local_way_are_normalised(re_world):
    from hooze.alerts import build_sinks
    w = re_world
    s = build_sinks(Settings(_env_file=None, alert_whatsapp_numbers="08012345678, +234 902 111 2222"), w.pdb, HybridFactory(w.adapter))
    assert s[0].numbers == ["2348012345678", "2349021112222"]


def test_errors_with_no_message_still_say_what_they_were():
    assert describe_exc(TimeoutError()) == "TimeoutError" and describe_exc(RuntimeError("boom")) == "boom" and len(describe_exc(RuntimeError("x" * 999))) == 200


# ── 8. alerts go out through a CONNECTED number ───────────────────────
async def test_alerts_use_a_connected_number_and_skip_dead_ones(re_world):
    w = re_world
    good = FakeAdapter()
    class Mixed(HybridFactory):
        def for_channel(self, ch):
            return good if ch["instance_name"] == "ops-number" else self._DEAD
    dead = FakeAdapter()
    async def closed(): return "close"
    dead.connection_status = closed
    Mixed._DEAD = dead
    await w.pdb.table("channels").insert({"tenant_id": w.tid, "kind": "whatsapp_evolution", "instance_name": "ops-number", "config": {}, "credentials_enc": "x", "secret_hash": "y", "status": "active"}).execute()
    sink = WhatsAppAlertSink(w.pdb, Mixed(w.adapter), ["2348011111111"])
    await sink.send("t", "ALERT BODY")
    assert [t for _, t in good.sent] == ["ALERT BODY"] and dead.sent == []


# ── 9. one typing wait, owned by the provider ─────────────────────────
async def test_a_message_waits_once_for_typing_and_the_provider_shows_the_typing_indicator_for_that_time():
    from hooze.runtime.messenger import Messenger
    slept, sent = [], []
    class Adapter:
        async def send_text(self, to, text, typing_delay=0.0): sent.append((text, typing_delay))
    class Store:
        async def record_outbound(self, *a, **k): pass
    class Ev:
        async def warn(self, *a, **k): pass
    async def fake_sleep(s): slept.append(s)
    m = Messenger(adapter=Adapter(), store=Store(), to="234", conversation_id="c", tenant_id="t", events=Ev(), trace_id="x", delay_range=(1.0, 2.5), sleep=fake_sleep)
    await m.text("hello there")
    assert slept == [] and len(sent) == 1 and 1.0 <= sent[0][1] <= 2.5                  # no local dead wait; the provider's typing_delay carries it
    await m.text("instant", typing=False)
    assert sent[1][1] == 0.0


async def test_a_flexible_answer_is_still_an_answer_and_reaches_the_team(re_world):
    w = re_world
    w.llm.say("Booking it.", ("request_booking", {"subject": "General consultation", "preferred_time": "whenever you're free"}), intent="booking")
    await w.send("whenever you're free is fine")
    assert len(bookings(w)) == 1 and bookings(w)[0]["preferred_text"] == "whenever you're free"


# ── 9. typing: one wait, owned by the provider ───────────────────────
async def test_a_message_waits_once_for_typing_not_twice_and_the_typing_time_reaches_the_provider(re_world):
    from hooze.runtime.messenger import Messenger
    w = re_world
    slept = []
    async def fake_sleep(s): slept.append(s)
    m = Messenger(adapter=w.adapter, store=None, to=CUSTOMER, conversation_id="c", tenant_id=w.tid, events=w.events, trace_id="t", delay_range=(1.0, 2.5), sleep=fake_sleep)
    class NoStore:
        async def record_outbound(self, *a, **k): pass
    m.store = NoStore()
    await m.text("Hello there")
    assert slept == [] and w.adapter.typing_delays and 1.0 <= w.adapter.typing_delays[-1] <= 2.5

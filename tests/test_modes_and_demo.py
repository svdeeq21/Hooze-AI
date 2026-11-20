"""Operating modes (after-hours, assist), the web test chat, demo links and the weekly owner report."""
from datetime import datetime, timedelta, timezone

from hooze.config_engine.normalize import parse_config
from hooze.config_engine.schema import Communication
from hooze.config_engine.validator import validate
from hooze.runtime.scheduler import run_tick
from tests.conftest import CUSTOMER, STAFF, food_config, make_world, realestate_config
from tests.test_api import PLATFORM, client, container

P = {"x-api-key": PLATFORM}
SESSION = "browser-session-0001"


def _mode(bc, mode, hours=None):
    bc.communication = Communication(tone=bc.communication.tone, greeting=bc.communication.greeting, rules=bc.communication.rules, mode=mode)
    if hours is not None:
        bc.hours = hours
    return bc


# ── config ─────────────────────────────────────────────────────────────
def test_mode_is_normalised_and_after_hours_needs_opening_hours():
    raw = realestate_config().model_dump(mode="json")
    raw["communication"]["mode"] = "After Hours"
    bc, fixes = parse_config(raw)
    assert bc.communication.mode == "after_hours" and fixes == []
    assert "hours.after_hours" in {i.code for i in validate(bc).blocking}               # real estate has no hours set
    raw["communication"]["mode"] = "turbo"
    bc, fixes = parse_config(raw)
    assert bc.communication.mode == "full_auto" and any("communication.mode" in f for f in fixes)


# ── after-hours mode ───────────────────────────────────────────────────
async def test_after_hours_mode_stays_quiet_while_open_and_answers_when_closed():
    w = await make_world(_mode(realestate_config(), "after_hours", {"default": ["09:00", "18:00"]}))
    out = await w.send("hello, are you there?")                     # 12:00 Lagos: the team is in
    assert out.status == "team_hours" and w.replies() == []
    assert [m for m in w.db.rows("messages") if m["direction"] == "inbound"][0]["content"] == "hello, are you there?"   # still stored
    w.clock.advance(hours=10)                                        # 22:00 Lagos: closed, the assistant covers
    w.llm.say("Hi! We're closed but I can help.")
    assert (await w.send("hello again")).status == "replied" and w.last_reply() == "Hi! We're closed but I can help."


async def test_after_hours_mode_still_takes_orders_while_the_business_is_closed():
    w = await make_world(_mode(food_config(), "after_hours", {"default": ["09:00", "21:00"]}))
    w.clock.advance(hours=11)                                        # 23:00 Lagos
    w.llm.say("Here's the menu!")
    out = await w.send("hi, what do you have?")
    assert out.status == "replied" and "outside our opening hours" not in " ".join(w.replies())


# ── assist mode ────────────────────────────────────────────────────────
async def test_assist_mode_drafts_every_reply_and_a_person_sends_it_once():
    w = await make_world(_mode(realestate_config(), "assist"))
    w.llm.say("We have a lovely 4-bed in Maitama.")
    await w.send("what do you have in Maitama?")
    assert w.replies() == []                                         # nothing reached WhatsApp
    (d,) = [r for r in w.db.rows("reply_drafts") if r["status"] == "pending"]
    assert d["content"] == "We have a lovely 4-bed in Maitama."
    async with client(container(w)) as http:
        listed = (await http.get(f"/v1/tenants/{w.tid}/drafts", headers=P)).json()["drafts"]
        assert [x["id"] for x in listed] == [d["id"]]
        r = await http.post(f"/v1/tenants/{w.tid}/drafts/{d['id']}/send", json={"text": "We have a lovely 4-bed terrace in Maitama 🏡"}, headers=P)
        again = await http.post(f"/v1/tenants/{w.tid}/drafts/{d['id']}/send", json={}, headers=P)
    assert r.status_code == 200 and r.json() == {"sent": True, "edited": True} and again.status_code == 409
    assert w.replies() == ["We have a lovely 4-bed terrace in Maitama 🏡"]                         # sent exactly once, as edited
    rec = [m for m in w.db.rows("messages") if m["sender_type"] == "bot"][-1]
    assert rec["content"] == "We have a lovely 4-bed terrace in Maitama 🏡" and rec["meta"]["approved_by"] == "platform" and rec["meta"]["edited"]


async def test_assist_mode_actions_still_happen_and_drafts_can_be_discarded():
    w = await make_world(_mode(realestate_config(), "assist"))
    w.llm.say("Booking!", ("request_booking", {"subject": "Maitama Terrace", "preferred_time": "Saturday 2pm"}))
    await w.send("Book Maitama Terrace for Saturday 2pm")
    assert len(w.db.rows("bookings")) == 1 and "New inspection request" in w.last_reply(STAFF)    # the action is real; staff told
    (d,) = w.db.rows("reply_drafts")
    async with client(container(w)) as http:
        assert (await http.post(f"/v1/tenants/{w.tid}/drafts/{d['id']}/discard", headers=P)).json() == {"discarded": True}
        assert (await http.post(f"/v1/tenants/{w.tid}/drafts/{d['id']}/send", json={}, headers=P)).status_code == 409
    assert w.replies() == []


# ── web test chat ──────────────────────────────────────────────────────
async def test_test_chat_runs_the_real_pipeline_and_never_touches_whatsapp(re_world):
    w = re_world                                                      # this client HAS a real WhatsApp number
    w.llm.say("Booking!", ("request_booking", {"subject": "Maitama Terrace", "preferred_time": "Saturday 2pm"}))
    async with client(container(w)) as http:
        r = await http.post(f"/v1/tenants/{w.tid}/test-chat", json={"session": SESSION, "text": "Book Maitama Terrace Saturday 2pm"}, headers=P)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "replied"
    assert "sent your inspection request" in body["replies"][-1]["text"]
    assert any("New inspection request" in a for a in body["owner_alerts"])                       # shown in the chat...
    assert w.adapter.sent == []                                                                   # ...never texted to staff or anyone
    assert body["state"]["booking"]["status"] == "pending" and any(e["event"] == "TURN_COMPLETED" for e in body["activity"])
    (contact,) = w.db.rows("contacts")
    assert contact["phone"].startswith("000") and contact["display_name"] == "Test customer"


async def test_test_chat_ignores_assist_and_after_hours_so_you_see_the_real_answer():
    w = await make_world(_mode(realestate_config(), "assist"))
    w.llm.say("Hello from the assistant")
    async with client(container(w)) as http:
        body = (await http.post(f"/v1/tenants/{w.tid}/test-chat", json={"session": SESSION, "text": "hi"}, headers=P)).json()
    assert [x["text"] for x in body["replies"]] == ["Hello from the assistant"] and w.db.rows("reply_drafts") == []


async def test_test_chat_checkout_is_simulated_and_pay_now_confirms_it(food_world):
    w = food_world
    async with client(container(w)) as http:
        async def say(text, *actions):
            w.llm.say("ok", *actions)
            return (await http.post(f"/v1/tenants/{w.tid}/test-chat", json={"session": SESSION, "text": text}, headers=P)).json()
        await say("2 large boxes with cheese", ("add_product", {"product_name": "Loaded Potato Box", "modifier_names": ["Cheese", "Large"], "quantity": 2}))
        await say("pickup", ("set_fulfillment", {"method": "PICKUP", "delivery_address": None}))
        out = await say("pay", ("checkout", {}))
        assert "https://pay.hooze.demo/" in out["replies"][-1]["text"] and out["state"]["payment_due"]
        assert w.payments.initialized == []                                                       # Paystack was never called
        paid = (await http.post(f"/v1/tenants/{w.tid}/test-chat/pay", json={"session": SESSION}, headers=P)).json()
    assert paid["state"]["order"]["status"] == "PAID" and any("New paid order" in a for a in paid["owner_alerts"])
    assert paid["replies"] and w.adapter.sent == []


async def test_clearing_test_chats_removes_only_test_customers(food_world):
    w = food_world
    w.llm.say("Hi real customer"); await w.send("hello")                                          # a real WhatsApp customer
    async with client(container(w)) as http:
        w.llm.say("ok", ("add_product", {"product_name": "Chapman", "modifier_names": []}))
        await http.post(f"/v1/tenants/{w.tid}/test-chat", json={"session": SESSION, "text": "one chapman"}, headers=P)
        assert len(w.db.rows("contacts")) == 2 and len(w.db.rows("orders")) == 1
        r = (await http.delete(f"/v1/tenants/{w.tid}/test-chat", headers=P)).json()
    assert r == {"customers": 1} and [c["phone"] for c in w.db.rows("contacts")] == [CUSTOMER] and w.db.rows("orders") == []


async def test_a_web_only_client_can_go_live_without_whatsapp_or_paystack_and_is_told_so(food_world):
    w = food_world
    async with client(container(w)) as http:
        t = (await http.post("/v1/platform/tenants", json={"slug": "web-only", "name": "Web Only"}, headers=P)).json()["tenant"]
        cfg = food_config().model_dump(mode="json")
        before = (await http.post(f"/v1/tenants/{t['id']}/config/validate", json=cfg, headers=P)).json()
        assert "integrations.channel" in {i["code"] for i in before["blocking"]}
        await http.post(f"/v1/tenants/{t['id']}/test-chat/enable", headers=P)
        after = (await http.post(f"/v1/tenants/{t['id']}/config/validate", json=cfg, headers=P)).json()
    assert after["ok"] and "integrations.whatsapp" in {i["code"] for i in after["warnings"]}


# ── demo links ─────────────────────────────────────────────────────────
async def test_a_demo_link_chats_with_no_api_key_within_its_allowance_and_can_be_switched_off(re_world):
    w = re_world
    async with client(container(w)) as http:
        link = (await http.post(f"/v1/tenants/{w.tid}/demo-links", json={"label": "Ada's agency", "days": 7, "max_messages": 10}, headers=P)).json()
        tok = link["token"]
        info = (await http.get(f"/v1/demo/{tok}")).json()
        assert info["business"] == w.tenant["name"] and info["live"] and info["messages_left"] == 10 and info["suggestions"]
        w.llm.say("Welcome to Praise Dynasty!")
        r = (await http.post(f"/v1/demo/{tok}/messages", json={"session": SESSION, "text": "hello"})).json()
        assert r["replies"][-1]["text"] == "Welcome to Praise Dynasty!" and r["messages_left"] == 9
        hist = (await http.get(f"/v1/demo/{tok}/history?session={SESSION}")).json()["messages"]
        assert [m["from"] for m in hist] == ["customer", "assistant"]
        assert (await http.get("/v1/demo/not-a-real-token")).status_code == 404
        lid = link["id"]
        assert (await http.delete(f"/v1/tenants/{w.tid}/demo-links/{lid}", headers=P)).status_code == 200
        assert (await http.post(f"/v1/demo/{tok}/messages", json={"session": SESSION, "text": "hi"})).status_code == 404
    stored = w.db.rows("demo_links")[0]
    assert stored["token_hash"] != tok and tok not in str(stored)                                # only the hash is kept


async def test_demo_links_stop_at_their_allowance_and_their_expiry(re_world):
    w = re_world
    async with client(container(w)) as http:
        tok = (await http.post(f"/v1/tenants/{w.tid}/demo-links", json={"days": 1, "max_messages": 10}, headers=P)).json()["token"]
        codes = [(await http.post(f"/v1/demo/{tok}/messages", json={"session": f"sess-{i:04d}-xxxx", "text": "hi"})).status_code for i in range(11)]
        assert codes == [200] * 10 + [429]
        tok2 = (await http.post(f"/v1/tenants/{w.tid}/demo-links", json={"days": 1, "max_messages": 10}, headers=P)).json()["token"]
        w.clock.advance(days=2)
        assert (await http.get(f"/v1/demo/{tok2}")).status_code == 410


# ── weekly owner report ────────────────────────────────────────────────
async def test_weekly_report_goes_out_once_on_monday_morning_and_leaves_out_test_chats(food_world):
    w = food_world
    w.llm.say("Hi!"); await w.send("hello")                                                       # one real customer message
    async with client(container(w)) as http:
        await http.post(f"/v1/tenants/{w.tid}/test-chat", json={"session": SESSION, "text": "test message"}, headers=P)
        preview = (await http.get(f"/v1/tenants/{w.tid}/reports/weekly", headers=P)).json()
    assert preview["numbers"]["messages"] == 1 and preview["numbers"]["chats"] == 1
    kw = dict(pdb=w.pdb, configs=w.configs, adapters=type("F", (), {"for_channel": lambda s, c: w.adapter})(), events=w.events, llm=w.llm,
              clock=w.clock, extras_for=lambda t: {}, weekly_reports=True)
    await run_tick(**kw)                                                                          # Thursday: nothing
    assert not any("Your Hooze week" in t for t in w.replies(STAFF))
    w.clock.now = datetime(2026, 9, 28, 7, 30, tzinfo=timezone.utc)                              # Monday 08:30 Lagos
    await run_tick(**kw)
    await run_tick(**kw)                                                                          # a second sweep must not resend
    reports = [t for t in w.replies(STAFF) if "Your Hooze week" in t]
    assert len(reports) == 1 and "message answered" in reports[0] and "paid order" in reports[0]

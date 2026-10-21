"""Operator alerts: the right events trigger them, a flapping fault sends ONE message, delivery failures never hurt a customer's turn."""
import asyncio
import time

import httpx
import pytest

from hooze.alerts import AlertManager, EmailAlertSink, RULES, Sink, WhatsAppAlertSink, build_sinks
from hooze.settings import Settings
from tests.test_api import PLATFORM, HybridFactory, RecPipeline, client, container

P = {"x-api-key": PLATFORM}


class Rec(Sink):
    def __init__(self, name="rec", fail=False, delay=0.0):
        self.name, self.fail, self.delay, self.sent = name, fail, delay, []

    async def send(self, title, body):
        if self.delay: await asyncio.sleep(self.delay)
        if self.fail: raise RuntimeError(f"{self.name} is down")
        self.sent.append((title, body))


def wire(w, *sinks, **kw):
    async def name_of(tid): return "Praise Dynasty"
    m = AlertManager(list(sinks), w.clock, name_of=name_of, inline=kw.pop("inline", True), **kw)
    m.bus = w.events
    w.events.sinks.append(m)
    return m


# ── rules ─────────────────────────────────────────────────────────────
async def test_a_failed_customer_message_alerts_with_the_client_name_the_detail_and_what_to_do(re_world):
    w, rec = re_world, Rec()
    wire(w, rec)
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid, error="database timeout", attempts=5)
    (title, body), = rec.sent
    assert "could not be processed" in title and "Client: Praise Dynasty" in body and "database timeout" in body and "Do: " in body and "Retry" in body and "(Lagos)" in body


async def test_ordinary_events_do_not_alert_and_alerts_do_not_alert_about_themselves(re_world):
    w, rec = re_world, Rec()
    wire(w, rec)
    await w.events.info("TURN_COMPLETED", tenant_id=w.tid)
    await w.events.warn("ESCALATED", tenant_id=w.tid)                 # a business-level event: the owner is told by WhatsApp, not the operator
    await w.events.info("ALERT_SENT", tenant_id=w.tid)
    assert rec.sent == []


@pytest.mark.parametrize("event", ["INBOUND_FAILED", "TURN_CRASHED", "PAYMENT_UNKNOWN_REFERENCE", "PAYMENT_AFTER_EXPIRED", "STAFF_ALERT_FAILED",
                                   "NO_ACTIVE_CONFIG", "CHANNEL_DISCONNECTED", "WEBHOOK_SET_FAILED", "TICK_FAILED"])
async def test_each_critical_event_raises_an_alert(re_world, event):
    w, rec = re_world, Rec()
    wire(w, rec)
    await w.events.error(event, tenant_id=w.tid)
    assert len(rec.sent) == 1


def test_every_alert_rule_has_advice_and_the_event_names_exist_in_the_code():
    import pathlib, re
    code = "\n".join(p.read_text() for p in pathlib.Path("hooze").rglob("*.py") if p.name != "alerts.py")
    emitted = set(re.findall(r'(?:warn|error|info)\(\s*f?"([A-Z_]+)', code)) | set(re.findall(r'f"(PAYMENT_AFTER_)\{', code))
    emitted |= {"PAYMENT_AFTER_EXPIRED", "PAYMENT_AFTER_CANCELLED"}
    for r in RULES:
        assert r.advice.strip() and r.title.strip()
        for e in r.events:
            assert e in emitted, f"rule {r.id} watches {e}, which nothing emits (a typo would silently disable the alert)"


# ── de-duplication and thresholds ─────────────────────────────────────
async def test_a_flapping_fault_sends_one_alert_per_window_and_says_how_many_were_held_back(re_world):
    w, rec = re_world, Rec()
    wire(w, rec)
    for _ in range(5):
        await w.events.error("INBOUND_FAILED", tenant_id=w.tid, error="x")
    assert len(rec.sent) == 1
    w.clock.advance(seconds=601)                                       # past the 10-minute window
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid, error="x")
    assert len(rec.sent) == 2 and "4 similar alert(s) held back" in rec.sent[1][1]


async def test_different_clients_and_different_problems_are_not_suppressed_by_each_other(re_world, food_world):
    w, other, rec = re_world, food_world, Rec()
    wire(w, rec)
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)
    await w.events.error("INBOUND_FAILED", tenant_id=other.tid)
    await w.events.error("TURN_CRASHED", tenant_id=w.tid)
    assert len(rec.sent) == 3


async def test_a_repeating_warning_alerts_only_after_the_threshold_inside_its_window(re_world):
    w, rec = re_world, Rec()
    wire(w, rec)
    for _ in range(2):
        await w.events.warn("LLM_FALLBACK_USED", tenant_id=w.tid)
    assert rec.sent == []                                              # 2 of 3: just a blip
    await w.events.warn("LLM_FALLBACK_USED", tenant_id=w.tid)
    assert len(rec.sent) == 1 and "primary AI provider" in rec.sent[0][0] and "Seen 3+ times" in rec.sent[0][1]


async def test_old_occurrences_age_out_of_the_threshold_window(re_world):
    w, rec = re_world, Rec()
    wire(w, rec)
    for _ in range(2):
        await w.events.warn("LLM_FALLBACK_USED", tenant_id=w.tid)
        w.clock.advance(seconds=400)                                   # each hit is 400s apart: never 3 inside 600s
    await w.events.warn("LLM_FALLBACK_USED", tenant_id=w.tid)
    assert rec.sent == []


async def test_the_configured_dedupe_window_overrides_the_rule_default(re_world):
    w, rec = re_world, Rec()
    wire(w, rec, dedupe_window_s=3600)
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)
    w.clock.advance(seconds=1800)
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)
    assert len(rec.sent) == 1


# ── delivery never hurts ──────────────────────────────────────────────
async def test_one_broken_destination_does_not_stop_the_others_and_the_failure_is_logged(re_world):
    w = re_world
    bad, good = Rec("email", fail=True), Rec("whatsapp")
    wire(w, bad, good)
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)
    assert len(good.sent) == 1 and "ALERT_SENT" in w.sink.names()


async def test_when_every_destination_fails_a_warning_is_logged_and_nothing_is_raised(re_world):
    w = re_world
    wire(w, Rec("a", fail=True), Rec("b", fail=True))
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)            # must not raise into the caller
    assert "ALERT_DELIVERY_FAILED" in w.sink.names()


async def test_alert_delivery_runs_in_the_background_and_never_slows_the_customers_turn(re_world):
    w, slow = re_world, Rec(delay=0.3)
    m = wire(w, slow, inline=False)
    t0 = time.monotonic()
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)
    assert time.monotonic() - t0 < 0.1 and slow.sent == []
    await m.drain()
    assert len(slow.sent) == 1


async def test_with_no_destinations_configured_nothing_happens_and_nothing_breaks(re_world):
    w = re_world
    m = wire(w)
    await w.events.error("INBOUND_FAILED", tenant_id=w.tid)
    assert m.describe() == [] and await m.send_test() == []


async def test_a_test_alert_reaches_every_destination_and_reports_each_result(re_world):
    w = re_world
    ok, bad = Rec("whatsapp"), Rec("email", fail=True)
    m = wire(w, ok, bad)
    res = await m.send_test()
    assert [r["ok"] for r in res] == [True, False] and "email is down" in res[1]["error"] and "test alert" in ok.sent[0][0].lower()


# ── destinations ──────────────────────────────────────────────────────
async def test_whatsapp_alerts_go_to_every_number_from_the_chosen_channel(re_world):
    w = re_world
    sink = WhatsAppAlertSink(w.pdb, HybridFactory(w.adapter), ["2348011111111", "2348022222222"], w.channel["id"])
    await sink.send("t", "hello operator")
    assert [(p, t) for p, t in w.adapter.sent if t == "hello operator"] == [("2348011111111", "hello operator"), ("2348022222222", "hello operator")]


async def test_whatsapp_alerts_tolerate_one_bad_number_but_raise_when_all_fail_or_no_channel(re_world):
    w = re_world
    calls = []
    async def flaky(phone, text, **k):
        calls.append(phone)
        if phone.endswith("1111"): raise RuntimeError("not on whatsapp")
    w.adapter.send_text = flaky
    sink = WhatsAppAlertSink(w.pdb, HybridFactory(w.adapter), ["2348011111111", "2348022222222"])
    await sink.send("t", "x")                                          # one delivered: success
    assert calls == ["2348011111111", "2348022222222"]
    only_bad = WhatsAppAlertSink(w.pdb, HybridFactory(w.adapter), ["2348011111111"])
    with pytest.raises(RuntimeError): await only_bad.send("t", "x")
    w.db.rows("channels")[0]["status"] = "disabled"
    with pytest.raises(RuntimeError, match="no active WhatsApp channel"): await sink.send("t", "x")


async def test_email_alerts_post_the_right_payload_to_resend_and_report_errors():
    seen = {}
    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["json"] = str(req.url), req.headers["authorization"], __import__("json").loads(req.content)
        return httpx.Response(200, json={"id": "e1"})
    mk = lambda timeout=15.0: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = EmailAlertSink("re_key", ["ops@hooze.com", "me@hooze.com"], "Hooze <a@hooze.com>", http_factory=mk)
    await sink.send("Subject here", "Body here")
    assert seen["url"] == "https://api.resend.com/emails" and seen["auth"] == "Bearer re_key"
    assert seen["json"] == {"from": "Hooze <a@hooze.com>", "to": ["ops@hooze.com", "me@hooze.com"], "subject": "Subject here", "text": "Body here"}
    bad = EmailAlertSink("k", ["a@b.c"], "x", http_factory=lambda timeout=15.0: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(422, text="invalid from"))))
    with pytest.raises(RuntimeError, match="Resend 422"): await bad.send("s", "b")


def test_build_sinks_follows_the_settings_and_email_needs_a_key(re_world):
    w = re_world
    mk = lambda **kw: build_sinks(Settings(_env_file=None, **kw), w.pdb, HybridFactory(w.adapter))
    assert mk() == []
    assert [s.name for s in mk(alert_whatsapp_numbers="+234 801 111 1111, 2348022222222")] == ["whatsapp"]
    assert mk(alert_whatsapp_numbers="+234 801 111 1111, 2348022222222")[0].numbers == ["2348011111111", "2348022222222"]
    assert mk(alert_email_to="a@b.com") == []                          # no API key: not usable, so not listed as configured
    assert sorted(s.name for s in mk(alert_whatsapp_numbers="2348011111111", alert_email_to="a@b.com", resend_api_key="k")) == ["email", "whatsapp"]


# ── operator API ──────────────────────────────────────────────────────
async def test_the_operator_can_send_a_test_alert_and_overview_lists_the_destinations(re_world):
    w, rec = re_world, Rec("whatsapp")
    c = container(w, pipeline=RecPipeline())
    c.alerts = wire(w, rec)
    async with client(c) as http:
        none = AlertManager([], w.clock)
        t = (await http.post("/v1/platform/alerts/test", headers=P)).json()
        ov = (await http.get("/v1/platform/overview", headers=P)).json()
        c.alerts = none
        unconfigured = (await http.post("/v1/platform/alerts/test", headers=P)).json()
        no_key = await http.post("/v1/platform/alerts/test")
    assert t == {"configured": True, "results": [{"sink": "whatsapp", "ok": True}]} and ov["alerts"] == {"configured": True, "destinations": ["whatsapp"]}
    assert unconfigured == {"configured": False, "results": []} and no_key.status_code == 401


async def test_an_unexpected_failure_while_composing_an_alert_is_contained(re_world):
    """The outer safety net: a malformed event must not raise out of the background delivery task."""
    w = re_world
    m = wire(w, Rec())
    class Evil:
        def __str__(self): raise ValueError("cannot render")
    rule = RULES[0]
    assert await m._deliver(rule, {"event": rule.id, "tenant_id": w.tid, "metadata": {"error": Evil()}}, 0) == []

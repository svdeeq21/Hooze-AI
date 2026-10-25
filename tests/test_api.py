"""HTTP layer through the real ASGI stack (httpx ASGITransport): auth fails closed, payments are exactly-once, tenants can't cross."""
import json

import httpx
import pytest

from hooze.api.app import create_app
from hooze.api.deps import Container
from hooze.channels.base import AdapterFactory
from hooze.security import Vault, sha256_hex
from hooze.settings import Settings
from tests.conftest import CUSTOMER, STAFF, food_config, make_world, realestate_config
from tests.test_ordering import _pending

PLATFORM, CRON = "platform-key", "cron-secret"


class HybridAdapter:
    """REAL Evolution verify/parse (that's what the webhook tests must exercise) + the recording fake for every outbound send."""
    def __init__(self, real, fake): self._real, self._fake = real, fake
    kind = "whatsapp_evolution"
    def verify(self, **kw): return self._real.verify(**kw)
    def parse(self, body): return self._real.parse(body)
    def __getattr__(self, name): return getattr(self._fake, name)


class HybridFactory:
    def __init__(self, fake): self.fake = fake
    def for_channel(self, channel):
        from hooze.channels.evolution import EvolutionAdapter
        return HybridAdapter(EvolutionAdapter("http://evo.invalid", "", channel["instance_name"]), self.fake)


class RecPipeline:
    def __init__(self): self.calls = []
    async def handle_inbound(self, channel, inbound, **kw): self.calls.append((channel, inbound))


def container(w, *, pipeline=None, has_payments=True, cron=CRON) -> Container:
    vault = Vault(Vault.generate_key())
    async def payments_for(tid):
        if not has_payments: raise LookupError("none")
        return w.payments
    settings = Settings(_env_file=None, platform_admin_key=PLATFORM, internal_cron_secret=cron, public_base_url="https://api.test")
    adapters, pipe = HybridFactory(w.adapter), pipeline or w.pipeline
    from hooze.runtime.inbox import Inbox
    from hooze.runtime.maintenance import Maintenance
    inbox = Inbox(w.pdb, pipe, adapters, w.events, settings, w.clock)
    return Container(settings=settings, pdb=w.pdb, llm=w.llm, events=w.events, adapters=adapters, configs=w.configs, pipeline=pipe,
                     vault=vault, payments_for=payments_for, clock=w.clock, inbox=inbox, maintenance=Maintenance(inbox, w.pdb, adapters, w.events, settings, w.clock))


def client(c) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(c)), base_url="http://test")


def wa_body(text="hi", instance="inst-acme", event="messages.upsert", msg_id="W1"):
    return {"event": event, "instance": instance, "data": {"key": {"remoteJid": f"{CUSTOMER}@s.whatsapp.net", "fromMe": False, "id": msg_id}, "pushName": "Ada",
                                                            "messageType": "conversation", "message": {"conversation": text}}}


# ── WhatsApp webhook: fail closed (audit C1) ───────────────────────────
async def test_webhook_rejects_missing_or_wrong_secret_and_never_reaches_the_pipeline(re_world):
    w, rec = re_world, RecPipeline()
    async with client(container(w, pipeline=rec)) as c:
        url = f"/v1/webhooks/whatsapp/{w.channel['id']}"
        assert (await c.post(url, json=wa_body())).status_code == 401
        assert (await c.post(url, json=wa_body(), headers={"x-hooze-secret": "wrong"})).status_code == 401
        assert (await c.post(url, json=wa_body(instance="someone-elses"), headers={"x-hooze-secret": "s3cret"})).status_code == 401
    assert rec.calls == [] and w.sink.names().count("WEBHOOK_AUTH_FAILED") == 3


async def test_webhook_accepts_header_and_hands_off_to_the_pipeline(re_world):
    w, rec = re_world, RecPipeline()
    async with client(container(w, pipeline=rec)) as c:
        url = f"/v1/webhooks/whatsapp/{w.channel['id']}"
        r1 = await c.post(url, json=wa_body("one", msg_id="W1"), headers={"x-hooze-secret": "s3cret"})
        ignored = await c.post(url, json=wa_body(event="connection.update"), headers={"x-hooze-secret": "s3cret"})
    assert (r1.json(), ignored.json()) == ({"status": "accepted"}, {"status": "ignored"})
    assert [i.text for _, i in rec.calls] == ["one"] and rec.calls[0][0]["tenant_id"] == w.tid


async def test_webhook_query_token_is_rejected_by_default_and_says_why(re_world):
    w, rec = re_world, RecPipeline()
    async with client(container(w, pipeline=rec)) as c:
        url = f"/v1/webhooks/whatsapp/{w.channel['id']}"
        r = await c.post(f"{url}?token=s3cret", json=wa_body("two", msg_id="W2"))
    assert r.status_code == 401 and rec.calls == []
    assert "WEBHOOK_QUERY_TOKEN_REJECTED" in w.sink.names()


async def test_webhook_query_token_works_only_when_explicitly_enabled(re_world):
    w, rec = re_world, RecPipeline()
    cont = container(w, pipeline=rec)
    cont.settings.webhook_allow_query_token = True
    async with client(cont) as c:
        r = await c.post(f"/v1/webhooks/whatsapp/{w.channel['id']}?token=s3cret", json=wa_body("two", msg_id="W2"))
    assert r.json() == {"status": "accepted"}


async def test_webhook_unknown_malformed_and_disabled_channels_are_404_or_400(re_world):
    w = re_world
    async with client(container(w, pipeline=RecPipeline())) as c:
        h = {"x-hooze-secret": "s3cret"}
        assert (await c.post("/v1/webhooks/whatsapp/00000000-0000-0000-0000-000000000000", json=wa_body(), headers=h)).status_code == 404
        assert (await c.post("/v1/webhooks/whatsapp/not-a-uuid", json=wa_body(), headers=h)).status_code == 404
        assert (await c.post(f"/v1/webhooks/whatsapp/{w.channel['id']}", content=b"{not json", headers=h)).status_code == 400
        w.db.rows("channels")[0]["status"] = "disabled"
        assert (await c.post(f"/v1/webhooks/whatsapp/{w.channel['id']}", json=wa_body(), headers=h)).status_code == 404


async def test_end_to_end_webhook_to_customer_reply(re_world):
    w = re_world
    w.llm.say("Welcome! What are you looking for?")
    async with client(container(w)) as c:                                          # real pipeline this time
        r = await c.post(f"/v1/webhooks/whatsapp/{w.channel['id']}", json=wa_body("hello"), headers={"x-hooze-secret": "s3cret"})
    assert r.status_code == 200 and "Welcome" in w.last_reply()


# ── payments webhook: verified, inline, exactly-once ───────────────────
def charge_event(ref): return json.dumps({"event": "charge.success", "data": {"reference": ref}})


async def test_payment_webhook_requires_a_valid_signature(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    async with client(container(w)) as c:
        url = f"/v1/webhooks/payments/{w.tid}/paystack"
        assert (await c.post(url, content=charge_event(ref))).status_code == 401
        assert (await c.post(url, content=charge_event(ref), headers={"x-test-sig": "forged"})).status_code == 401
        assert (await c.post(f"/v1/webhooks/payments/{w.tid}/flutterwave", content=charge_event(ref), headers={"x-test-sig": "valid"})).status_code == 401
    assert w.db.rows("orders")[0]["status"] == "PAYMENT_PENDING"


async def test_payment_webhook_confirms_once_even_when_the_provider_redelivers(food_world):
    w = food_world
    await _pending(w)
    ref, before = w.db.rows("payments")[0]["reference"], len(w.replies(STAFF))
    async with client(container(w)) as c:
        url, h = f"/v1/webhooks/payments/{w.tid}/paystack", {"x-test-sig": "valid"}
        first = await c.post(url, content=charge_event(ref), headers=h)
        again = await c.post(url, content=charge_event(ref), headers=h)
        other = await c.post(url, content=json.dumps({"event": "transfer.success", "data": {}}), headers=h)
    assert (first.json(), again.json(), other.json()) == ({"status": "confirmed"}, {"status": "noop"}, {"status": "ignored"})
    assert w.db.rows("orders")[0]["status"] == "PAID" and len(w.replies(STAFF)) == before + 1


async def test_payment_webhook_returns_5xx_on_failure_so_the_provider_retries(food_world):
    w = food_world
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    async def boom(r): raise RuntimeError("paystack verify down")
    w.payments.verify = boom
    async with client(container(w)) as c:
        r = await c.post(f"/v1/webhooks/payments/{w.tid}/paystack", content=charge_event(ref), headers={"x-test-sig": "valid"})
    assert r.status_code == 500 and "PAYMENT_CONFIRM_FAILED" in w.sink.names() and w.db.rows("orders")[0]["status"] == "PAYMENT_PENDING"


async def test_payment_webhook_for_a_tenant_without_payments_is_404(food_world):
    async with client(container(food_world, has_payments=False)) as c:
        assert (await c.post(f"/v1/webhooks/payments/{food_world.tid}/paystack", content=b"{}")).status_code == 404


# ── cron ───────────────────────────────────────────────────────────────
async def test_tick_is_protected_and_fails_closed_when_the_secret_is_unset(re_world):
    w = re_world
    async with client(container(w)) as c:
        assert (await c.post("/internal/tick")).status_code == 401
        assert (await c.post("/internal/tick", headers={"x-cron-secret": "nope"})).status_code == 401
        ok = await c.post("/internal/tick", headers={"x-cron-secret": CRON})
    assert ok.status_code == 200 and ok.json()["tenants"] == 1 and w.tid in ok.json()["results"]
    async with client(container(w, cron="")) as c:                                   # misconfigured deployment must not be open
        assert (await c.post("/internal/tick", headers={"x-cron-secret": ""})).status_code == 401
        assert (await c.post("/internal/tick")).status_code == 401


async def test_one_tenants_failure_does_not_stop_the_others_ticks():
    a = await make_world(realestate_config(), tenant_name="acme")
    b = await make_world(food_config(), tenant_name="bolt", db=a.db, clock=a.clock)
    a.db.rows("tenant_configs")[0]["compiled"] = {"broken": True}                  # corrupt tenant A's config
    async with client(container(b)) as c:
        r = (await c.post("/internal/tick", headers={"x-cron-secret": CRON})).json()
    assert "error" in r["results"][a.tid] and "ordering" in r["results"][b.tid] and "TICK_FAILED" in b.sink.names()


# ── admin auth + tenant isolation ──────────────────────────────────────
async def test_platform_endpoints_need_the_platform_key(re_world):
    async with client(container(re_world)) as c:
        body = {"slug": "new-co", "name": "New Co"}
        assert (await c.post("/v1/platform/tenants", json=body)).status_code == 401
        assert (await c.post("/v1/platform/tenants", json=body, headers={"x-api-key": "garbage"})).status_code == 401
        assert (await c.post("/v1/platform/tenants", json={"slug": "X!", "name": "n"}, headers={"x-api-key": PLATFORM})).status_code == 422
        r = await c.post("/v1/platform/tenants", json=body, headers={"x-api-key": PLATFORM})
        tenant_key = r.json()["api_key"]
        assert r.status_code == 200 and re_world.db.rows("api_keys")[-1]["key_hash"] == sha256_hex(tenant_key) and tenant_key not in json.dumps(re_world.db.tables["api_keys"])
        assert (await c.post("/v1/platform/tenants", json={"slug": "x2", "name": "n"}, headers={"x-api-key": tenant_key})).status_code == 403    # tenant key != platform


async def test_a_tenant_key_reaches_only_its_own_tenant_and_revocation_works():
    a = await make_world(realestate_config(), tenant_name="acme")
    b = await make_world(food_config(), tenant_name="bolt", db=a.db, clock=a.clock)
    async with client(container(a)) as c:
        ka = (await c.post("/v1/platform/tenants", json={"slug": "ka", "name": "K"}, headers={"x-api-key": PLATFORM})).json()
        key, kt = ka["api_key"], ka["tenant"]["id"]
        assert (await c.get(f"/v1/tenants/{kt}/conversations", headers={"x-api-key": key})).status_code == 200
        for other in (a.tid, b.tid):
            assert (await c.get(f"/v1/tenants/{other}/conversations", headers={"x-api-key": key})).status_code == 403    # cannot address another tenant
        assert (await c.get(f"/v1/tenants/{a.tid}/conversations", headers={"x-api-key": PLATFORM})).status_code == 200    # platform can
        a.db.rows("api_keys")[-1]["revoked_at"] = "2026-09-24T00:00:00+00:00"
        assert (await c.get(f"/v1/tenants/{kt}/conversations", headers={"x-api-key": key})).status_code == 401


# ── onboarding to live, through the API only ───────────────────────────
async def test_full_onboarding_flow_create_connect_activate_then_serve_a_customer():
    w = await make_world(realestate_config(), tenant_name="seed")                    # only used for shared fakes
    c_ = container(w)
    async with client(c_) as c:
        P = {"x-api-key": PLATFORM}
        t = (await c.post("/v1/platform/tenants", json={"slug": "bright-smile", "name": "Bright Smile"}, headers=P)).json()
        tid, T = t["tenant"]["id"], {"x-api-key": t["api_key"]}

        bc = realestate_config(); bc.identity.name = "Bright Smile Dental"; bc.booking.subject_label = "appointment"
        payload = json.loads(bc.model_dump_json())
        payload["integrations"]["channel_connected"] = True                          # client CLAIMS a channel exists...
        r = await c.post(f"/v1/tenants/{tid}/config/validate", json=payload, headers=T)
        assert r.status_code == 200 and "integrations.channel" in {i["code"] for i in r.json()["blocking"]}           # ...the server checks the database instead
        assert (await c.post(f"/v1/tenants/{tid}/config/activate", json=payload, headers=P)).status_code == 422

        ch = (await c.post(f"/v1/platform/tenants/{tid}/channels", json={"instance_name": "bright", "base_url": "http://evo.test", "api_key": "EVO-KEY-123"}, headers=P)).json()
        row = next(r for r in w.db.rows("channels") if r["id"] == ch["channel_id"])
        assert row["secret_hash"] == sha256_hex(ch["webhook_header"]["x-hooze-secret"]) and row["credentials_enc"] != "EVO-KEY-123"
        assert c_.vault.decrypt(row["credentials_enc"]) == "EVO-KEY-123" and ch["webhook_url"] == f"https://api.test/v1/webhooks/whatsapp/{ch['channel_id']}"

        w.llm.embed_fail = True
        assert (await c.post(f"/v1/tenants/{tid}/config/activate", json=payload, headers=P)).status_code == 503         # dependency outage != bad config
        w.llm.embed_fail = False
        act = await c.post(f"/v1/tenants/{tid}/config/activate", json=payload, headers=P)
        assert act.status_code == 200 and act.json()["version"] == 1 and act.json()["report"]["ok"]
        assert next(t for t in w.db.rows("tenants") if t["id"] == tid)["status"] == "active"

        # the brand-new tenant now serves a customer via its own webhook URL + secret
        w.llm.say("Welcome to Bright Smile Dental! What brings you in?")
        wh = await c.post(f"/v1/webhooks/whatsapp/{ch['channel_id']}", json=wa_body("hello", instance="bright"), headers=ch["webhook_header"])
        assert wh.status_code == 200
    assert "Welcome to Bright Smile Dental" in w.last_reply() and "Bright Smile Dental" in w.llm.calls[-1]["system"]


async def test_paystack_connection_is_stored_encrypted_and_validation_uses_the_real_flag():
    w = await make_world(food_config(), tenant_name="bolt")
    c_ = container(w)
    async with client(c_) as c:
        T = {"x-api-key": PLATFORM}
        bc = json.loads(food_config().model_dump_json()); bc["integrations"]["payments_connected"] = True       # a claim with no integration behind it
        blocking = lambda r: {i["code"] for i in r.json()["blocking"]}
        r = await c.post(f"/v1/tenants/{w.tid}/config/validate", json=bc, headers=T)
        assert "integrations.payments_connected" in blocking(r)                                                 # the DB has no integration: the claim is ignored
        conn = await c.post(f"/v1/tenants/{w.tid}/integrations/paystack", json={"secret_key": "sk_live_SECRET"}, headers=T)
        assert conn.json()["webhook_url"] == f"https://api.test/v1/webhooks/payments/{w.tid}/paystack"
        assert "integrations.payments_connected" not in blocking(await c.post(f"/v1/tenants/{w.tid}/config/validate", json=bc, headers=T))   # now it's real
        stored = w.db.rows("tenant_integrations")[0]["credentials_enc"]
        assert "sk_live_SECRET" not in stored and c_.vault.decrypt(stored) == "sk_live_SECRET"
        await c.post(f"/v1/tenants/{w.tid}/integrations/paystack", json={"secret_key": "sk_live_NEW"}, headers=T)     # rotate
        assert len(w.db.rows("tenant_integrations")) == 1


async def test_operator_can_pause_and_resume_a_conversation_and_decide_bookings(re_world):
    w = re_world
    w.llm.say("Booking!", ("request_booking", {"subject": "Maitama Terrace", "preferred_time": "Saturday 2pm"}))
    await w.send("book it")
    conv, bid = w.db.rows("conversations")[0]["id"], w.db.rows("bookings")[0]["id"]
    async with client(container(w)) as c:
        H = {"x-api-key": PLATFORM}
        assert (await c.post(f"/v1/tenants/{w.tid}/conversations/{conv}/pause", headers=H)).json() == {"status": "handoff"}
        assert w.db.rows("contacts")[0]["ai_paused"] is True
        assert (await c.post(f"/v1/tenants/{w.tid}/conversations/{conv}/resume", headers=H)).json() == {"status": "active"}
        assert (await c.post(f"/v1/tenants/{w.tid}/conversations/{conv}/explode", headers=H)).status_code == 404
        assert len((await c.get(f"/v1/tenants/{w.tid}/bookings", headers=H)).json()["bookings"]) == 1
        assert (await c.post(f"/v1/tenants/{w.tid}/bookings/{bid}/confirm", json={"when": "sometime"}, headers=H)).status_code == 422
        assert (await c.post(f"/v1/tenants/{w.tid}/bookings/{bid}/confirm", json={"when": "2026-10-03T14:00:00+01:00"}, headers=H)).status_code == 200
        assert (await c.post(f"/v1/tenants/{w.tid}/bookings/{bid}/confirm", json={"when": "2026-10-04T14:00:00+01:00"}, headers=H)).status_code == 409   # already decided
        assert (await c.post(f"/v1/tenants/{w.tid}/bookings/{bid}/decline", headers=H)).status_code == 409
    assert "Confirmed" in w.last_reply()


# ── usage metering ─────────────────────────────────────────────────────
async def test_usage_endpoint_aggregates_metered_events_and_message_counts(re_world):
    w = re_world
    for kind, qty in (("llm_call", 1), ("llm_call", 1), ("embedding", 1)):
        await w.tdb.table("usage_events").insert({"kind": kind, "quantity": qty, "metadata": {}}).execute()
    w.llm.say("Hi!"); await w.send("hello")
    async with client(container(w)) as c:
        r = (await c.get(f"/v1/tenants/{w.tid}/usage", headers={"x-api-key": PLATFORM})).json()
        future = (await c.get(f"/v1/tenants/{w.tid}/usage?since=2099-01-01T00:00:00%2B00:00", headers={"x-api-key": PLATFORM})).json()
    assert r["metered"] == {"llm_call": 2.0, "embedding": 1.0} and r["messages_in"] == 1 and r["messages_out_bot"] == 1
    assert future["metered"] == {} and future["messages_in"] == 0


# ── near-miss configs are repaired, not rejected ────────────────────────
async def test_validate_repairs_a_sloppy_config_and_says_what_it_fixed_and_rejects_non_objects_readably():
    import pathlib
    w = await make_world(realestate_config(), tenant_name="seed")
    async with client(container(w)) as c:
        P = {"x-api-key": PLATFORM}
        t = (await c.post("/v1/platform/tenants", json={"slug": "sloppy-co", "name": "Sloppy Co"}, headers=P)).json()
        tid, T = t["tenant"]["id"], {"x-api-key": t["api_key"]}
        sloppy = json.loads((pathlib.Path(__file__).parent / "fixtures" / "broken_7_trays.json").read_text())
        r = await c.post(f"/v1/tenants/{tid}/config/validate", json=sloppy, headers=T)
        assert r.status_code == 200                                                    # used to be a 422 (and a UI crash)
        assert any("qualification_slots[0]" in f for f in r.json()["normalized"])
        assert "handoff.staff_phones" in {i["code"] for i in r.json()["blocking"]}     # still asks for what it can't invent
        bad = await c.post(f"/v1/tenants/{tid}/config/validate", json=["nope"], headers=T)
        assert bad.status_code == 422

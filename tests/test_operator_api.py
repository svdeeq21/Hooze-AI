"""The operator API (what the dashboard calls): auth boundaries, no secret leakage, WhatsApp connect flow, human takeover, order flow."""
import json

import pytest

from hooze.api import operator as operator_mod
from hooze.security import sha256_hex
from hooze.settings import Settings
from tests.conftest import CUSTOMER, STAFF
from tests.test_api import PLATFORM, client, container, wa_body
from tests.test_capabilities import _request, bookings, idle_discovery
from tests.test_ordering import _pending

P = {"x-api-key": PLATFORM}


async def tenant_key(w, label="t") -> str:
    key = f"tk-{label}"
    await w.pdb.table("api_keys").insert({"tenant_id": w.tid, "key_hash": sha256_hex(key), "label": label}).execute()
    return key


# ── auth boundaries ───────────────────────────────────────────────────
@pytest.mark.parametrize("method, path", [("GET", "/v1/platform/overview"), ("GET", "/v1/platform/tenants"), ("GET", "/v1/platform/events"), ("POST", "/v1/platform/tick")])
async def test_platform_routes_reject_missing_wrong_and_tenant_keys(re_world, method, path):
    w = re_world
    async with client(container(w)) as c:
        assert (await c.request(method, path)).status_code == 401
        assert (await c.request(method, path, headers={"x-api-key": "nope"})).status_code == 401
        assert (await c.request(method, path, headers={"x-api-key": await tenant_key(w)})).status_code == 403


async def test_a_tenant_key_reads_its_own_data_but_never_another_tenants(re_world, food_world):
    a, b = re_world, food_world
    await _pending(b)
    key = await tenant_key(a)
    async with client(container(a)) as c:
        assert (await c.get(f"/v1/tenants/{a.tid}/orders", headers={"x-api-key": key})).status_code == 200
        assert (await c.get(f"/v1/tenants/{b.tid}/orders", headers={"x-api-key": key})).status_code == 403
        assert (await c.get(f"/v1/tenants/{b.tid}/stats", headers={"x-api-key": key})).status_code == 403


# ── fleet + detail never leak secrets ─────────────────────────────────
async def test_overview_list_and_detail_work_and_never_expose_credentials_or_secret_hashes(re_world):
    w = re_world
    async with client(container(w)) as c:
        ov = (await c.get("/v1/platform/overview", headers=P)).json()
        assert ov["tenants"]["total"] == 1 and ov["tenants"]["active"] == 1 and ov["platform"]["shared_state"].startswith("in-process")
        lst = (await c.get("/v1/platform/tenants", headers=P)).json()["tenants"]
        assert lst[0]["id"] == w.tid and lst[0]["channels"] == 1 and lst[0]["active_version"] == 1
        raw = (await c.get(f"/v1/platform/tenants/{w.tid}", headers=P)).text
        d = json.loads(raw)
        assert d["channels"][0]["connection"] == "CONNECTED" and d["active_config"]["version"] == 1 and d["channels"][0]["webhook_url"].endswith(w.channel["id"])
        assert "credentials_enc" not in raw and "secret_hash" not in raw and "key_hash" not in raw
        assert (await c.get("/v1/platform/tenants/00000000-0000-0000-0000-000000000000", headers=P)).status_code == 404


async def test_suspending_a_tenant_stops_the_assistant_and_reactivating_restores_it(re_world):
    w = re_world
    async with client(container(w)) as c:
        assert (await c.patch(f"/v1/platform/tenants/{w.tid}", headers=P, json={"status": "suspended"})).status_code == 200
        assert (await w.send("hi")).detail == "tenant_inactive"
        assert (await c.patch(f"/v1/platform/tenants/{w.tid}", headers=P, json={"status": "bogus"})).status_code == 422
        await c.patch(f"/v1/platform/tenants/{w.tid}", headers=P, json={"status": "active"})
        assert (await w.send("hello again")).status == "replied"


async def test_extra_api_keys_can_be_created_once_used_and_revoked(re_world):
    w = re_world
    async with client(container(w)) as c:
        made = (await c.post(f"/v1/platform/tenants/{w.tid}/api-keys", headers=P, json={"label": "ngozi"})).json()
        H = {"x-api-key": made["api_key"]}
        assert (await c.get(f"/v1/tenants/{w.tid}/stats", headers=H)).status_code == 200
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}/api-keys/{made['id']}", headers=P)).status_code == 200
        assert (await c.get(f"/v1/tenants/{w.tid}/stats", headers=H)).status_code == 401
        listed = (await c.get(f"/v1/platform/tenants/{w.tid}", headers=P)).json()["api_keys"]
        assert any(k["id"] == made["id"] and k["revoked_at"] for k in listed) and "api_key" not in json.dumps(listed)


# ── WhatsApp connect flow ─────────────────────────────────────────────
async def test_provision_creates_the_channel_points_evolution_at_us_and_the_secret_really_works(re_world, monkeypatch):
    w = re_world
    created = []
    async def fake_create(base, key, name, http_factory=None): created.append((base, name))
    monkeypatch.setattr(operator_mod, "create_evolution_instance", fake_create)
    async with client(container(w)) as c:
        r = (await c.post(f"/v1/platform/tenants/{w.tid}/channels/provision", headers=P, json={
            "instance_name": "newinst", "base_url": "http://evo:8080", "api_key": "evo-key", "create_instance": True,
            "webhook_base_url": "http://host.docker.internal:8000"})).json()
    assert created == [("http://evo:8080", "newinst")] and r["webhook_configured"] is True
    url, headers = w.adapter.webhooks[-1]
    assert url == r["webhook_url"] and url.startswith("http://host.docker.internal:8000/v1/webhooks/whatsapp/")
    row = next(ch for ch in w.db.rows("channels") if ch["instance_name"] == "newinst")
    assert row["secret_hash"] == sha256_hex(headers["x-hooze-secret"]) and "evo-key" not in json.dumps(row)          # key stored encrypted only
    from hooze.channels.evolution import EvolutionAdapter
    assert EvolutionAdapter("http://x", "", "newinst").verify(provided_secret=headers["x-hooze-secret"], body={"instance": "newinst"}, channel=row)


async def test_provision_survives_a_webhook_failure_and_reports_it_instead_of_pretending(re_world):
    w = re_world
    async def boom(url, headers): raise RuntimeError("evolution unreachable")
    w.adapter.set_webhook = boom
    async with client(container(w)) as c:
        r = (await c.post(f"/v1/platform/tenants/{w.tid}/channels/provision", headers=P, json={"instance_name": "x2", "base_url": "http://evo", "api_key": "k"})).json()
    assert r["webhook_configured"] is False and "Reconnect webhook" in r["note"] and "WEBHOOK_SET_FAILED" in w.sink.names()


async def test_failed_instance_creation_is_a_502_and_creates_no_channel(re_world, monkeypatch):
    w = re_world
    async def refuse(*a, **k): raise RuntimeError("Evolution create 403: name in use")
    monkeypatch.setattr(operator_mod, "create_evolution_instance", refuse)
    before = len(w.db.rows("channels"))
    async with client(container(w)) as c:
        r = await c.post(f"/v1/platform/tenants/{w.tid}/channels/provision", headers=P, json={"instance_name": "dup", "base_url": "http://evo", "api_key": "k", "create_instance": True})
    assert r.status_code == 502 and "name in use" in r.text and len(w.db.rows("channels")) == before


async def test_reconnect_rotates_the_secret_but_a_failure_keeps_the_old_working_one(re_world):
    w = re_world
    old_hash = w.channel["secret_hash"]
    async with client(container(w)) as c:
        ok = await c.post(f"/v1/platform/tenants/{w.tid}/channels/{w.channel['id']}/webhook", headers=P, json={})
        assert ok.status_code == 200
        new_secret = w.adapter.webhooks[-1][1]["x-hooze-secret"]
        assert w.db.rows("channels")[0]["secret_hash"] == sha256_hex(new_secret) != old_hash
        kept = w.db.rows("channels")[0]["secret_hash"]
        async def boom(url, headers): raise RuntimeError("down")
        w.adapter.set_webhook = boom
        bad = await c.post(f"/v1/platform/tenants/{w.tid}/channels/{w.channel['id']}/webhook", headers=P, json={})
        assert bad.status_code == 502 and w.db.rows("channels")[0]["secret_hash"] == kept        # Evolution still holds the matching secret


async def test_channel_status_and_qr_and_cross_tenant_channel_is_404(re_world, food_world):
    w = re_world
    async with client(container(w)) as c:
        assert (await c.get(f"/v1/platform/tenants/{w.tid}/channels/{w.channel['id']}/status", headers=P)).json() == {"connection": "CONNECTED"}
        qr = (await c.get(f"/v1/platform/tenants/{w.tid}/channels/{w.channel['id']}/qr", headers=P)).json()
        assert qr["qr_base64"].startswith("data:image/png") and qr["pairing_code"]
        assert (await c.get(f"/v1/platform/tenants/{food_world.tid}/channels/{w.channel['id']}/qr", headers=P)).status_code == 404


# ── conversations: history + human takeover ───────────────────────────
async def test_conversation_list_has_contacts_and_history_is_chronological(re_world):
    w = re_world
    await w.send("hi")
    async with client(container(w)) as c:
        convs = (await c.get(f"/v1/tenants/{w.tid}/conversations", headers=P)).json()["conversations"]
        assert convs[0]["contact"]["phone"] == CUSTOMER and convs[0]["contact"]["display_name"] == "Ada"
        hist = (await c.get(f"/v1/tenants/{w.tid}/conversations/{convs[0]['id']}/messages", headers=P)).json()
        assert [m["sender_type"] for m in hist["messages"]][:2] == ["contact", "bot"] and hist["contact"]["phone"] == CUSTOMER
        assert (await c.get(f"/v1/tenants/{w.tid}/conversations/{w.tid}/messages", headers=P)).status_code == 404


async def test_a_human_reply_is_sent_recorded_as_human_and_pauses_the_assistant(re_world):
    w = re_world
    await w.send("hi")
    cid = w.db.rows("conversations")[0]["id"]
    n = len(w.replies())
    async with client(container(w)) as c:
        r = await c.post(f"/v1/tenants/{w.tid}/conversations/{cid}/send", headers=P, json={"text": "Hi Ada, Sadiq here — I'll take it from here."})
    assert r.status_code == 200 and r.json()["assistant_paused"] and len(w.replies()) == n + 1 and "Sadiq here" in w.last_reply()
    assert any(m["sender_type"] == "human" and "Sadiq here" in m["content"] for m in w.db.rows("messages"))
    assert w.db.rows("contacts")[0]["ai_paused"] is True and (await w.send("thanks")).status == "paused"        # the bot stays quiet


async def test_a_failed_human_send_is_a_502_and_records_nothing(re_world):
    w = re_world
    await w.send("hi")
    cid = w.db.rows("conversations")[0]["id"]
    async def down(*a, **k): raise RuntimeError("whatsapp disconnected")
    w.adapter.send_text = down
    before = len(w.db.rows("messages"))
    async with client(container(w)) as c:
        r = await c.post(f"/v1/tenants/{w.tid}/conversations/{cid}/send", headers=P, json={"text": "hello?"})
        assert (await c.post(f"/v1/tenants/{w.tid}/conversations/{cid}/send", headers=P, json={"text": ""})).status_code == 422
    assert r.status_code == 502 and len(w.db.rows("messages")) == before and w.db.rows("contacts")[0]["ai_paused"] is False


# ── bookings listing ──────────────────────────────────────────────────
async def test_bookings_can_be_listed_by_status_or_all_with_the_customer_attached(re_world):
    w = re_world
    await _request(w)
    async with client(container(w)) as c:
        b = (await c.get(f"/v1/tenants/{w.tid}/bookings?status=all", headers=P)).json()["bookings"]
        assert b[0]["contact"]["phone"] == CUSTOMER and b[0]["status"] == "pending"
        await c.post(f"/v1/tenants/{w.tid}/bookings/{b[0]['id']}/confirm", headers=P, json={"when": "2026-10-03T14:00:00"})
        assert (await c.get(f"/v1/tenants/{w.tid}/bookings", headers=P)).json()["bookings"] == []
        assert (await c.get(f"/v1/tenants/{w.tid}/bookings?status=confirmed", headers=P)).json()["bookings"][0]["id"] == b[0]["id"]


# ── orders ────────────────────────────────────────────────────────────
async def _paid(w):
    await _pending(w)
    ref = w.db.rows("payments")[0]["reference"]
    await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00})
    return w.db.rows("orders")[0]["id"]


async def test_orders_list_and_detail_show_items_payment_and_customer(food_world):
    w = food_world
    oid = await _paid(w)
    async with client(container(w)) as c:
        lst = (await c.get(f"/v1/tenants/{w.tid}/orders", headers=P)).json()["orders"]
        assert lst[0]["id"] == oid and lst[0]["status"] == "PAID" and lst[0]["contact"]["phone"] == CUSTOMER
        d = (await c.get(f"/v1/tenants/{w.tid}/orders/{oid}", headers=P)).json()
        assert d["items"][0]["product_name"] == "Loaded Potato Box" and d["payments"][0]["status"] == "success" and "credentials" not in json.dumps(d)


async def test_order_status_flow_is_enforced_and_ready_tells_the_customer(food_world):
    w = food_world
    oid = await _paid(w)
    async with client(container(w)) as c:
        url = f"/v1/tenants/{w.tid}/orders/{oid}/status"
        assert (await c.post(url, headers=P, json={"status": "PAID"})).status_code == 422                       # not a staff target
        n = len(w.replies())
        assert (await c.post(url, headers=P, json={"status": "PREPARING"})).status_code == 200
        assert len(w.replies()) == n                                                                           # no message for PREPARING
        r = await c.post(url, headers=P, json={"status": "READY"})
        assert r.json()["customer_notified"] is True and "ready for pickup" in w.last_reply()
        assert (await c.post(url, headers=P, json={"status": "PREPARING"})).status_code == 409                  # can't go backwards
        assert (await c.post(url, headers=P, json={"status": "COMPLETED"})).status_code == 200
        assert (await c.post(url, headers=P, json={"status": "CANCELLED"})).status_code == 409                  # finished is final
        assert (await c.post(f"/v1/tenants/{w.tid}/orders/{w.tid}/status", headers=P, json={"status": "READY"})).status_code == 404
    assert w.db.rows("orders")[0]["status"] == "COMPLETED" and "ORDER_STATUS_CHANGED" in w.sink.names()


async def test_an_unpaid_order_cannot_be_marked_ready(food_world):
    w = food_world
    await _pending(w)
    oid = w.db.rows("orders")[0]["id"]
    async with client(container(w)) as c:
        assert (await c.post(f"/v1/tenants/{w.tid}/orders/{oid}/status", headers=P, json={"status": "READY"})).status_code == 409
        pend = (await c.get(f"/v1/tenants/{w.tid}/orders?status=PAYMENT_PENDING", headers=P)).json()["orders"]
        assert len(pend) == 1 and len((await c.get(f"/v1/tenants/{w.tid}/orders?status=PAID", headers=P)).json()["orders"]) == 0


# ── menu ──────────────────────────────────────────────────────────────
async def test_menu_lists_with_options_and_sold_out_toggle_is_respected_by_the_assistant(food_world):
    w = food_world
    async with client(container(w)) as c:
        prods = (await c.get(f"/v1/tenants/{w.tid}/products", headers=P)).json()["products"]
        box = next(p for p in prods if p["name"] == "Loaded Potato Box")
        assert {g["name"] for g in box["groups"]} == {"Size", "Toppings", "Extras"}
        assert (await c.patch(f"/v1/tenants/{w.tid}/products/{box['id']}", headers=P, json={"available": False})).json()["available"] is False
        bacon = next(m for g in box["groups"] for m in g["modifiers"] if m["name"] == "Bacon")
        assert (await c.patch(f"/v1/tenants/{w.tid}/modifiers/{bacon['id']}", headers=P, json={"available": False})).status_code == 200
        assert (await c.patch(f"/v1/tenants/{w.tid}/products/{w.tid}", headers=P, json={"available": False})).status_code == 404
    cat = await w.cap("ordering").load_catalog()
    assert [p["name"] for p in cat] == ["Chapman"]                                       # the bot now sees the box as unavailable


# ── stats / nudges / events / tick / config ───────────────────────────
async def test_stats_summarise_conversations_messages_orders_and_revenue(food_world):
    w = food_world
    await _paid(w)
    async with client(container(w)) as c:
        s = (await c.get(f"/v1/tenants/{w.tid}/stats", headers=P)).json()
    assert s["conversations"]["active"] == 1 and s["messages_24h"]["inbound"] >= 2 and s["messages_24h"]["bot_replies"] >= 1
    assert s["orders"]["PAID"] == 1 and s["orders"]["PAYMENT_PENDING"] == 0 and s["revenue_minor"] == {"total": 9_000_00, "last_7d": 9_000_00, "truncated": False}


async def test_nudge_reset_rewinds_open_conversations_but_never_re_enrols_opted_out_contacts(re_world):
    w = re_world
    await idle_discovery(w)
    cid = w.db.rows("conversations")[0]["id"]
    await w.tdb.table("nudge_state").insert({"conversation_id": cid, "status": "IDLE", "step": 2, "last_nudge_at": w.clock().isoformat()}).execute()
    other = (await w.tdb.table("contacts").insert({"phone": "2348022222222"}).execute()).data[0]
    conv2 = (await w.tdb.table("conversations").insert({"contact_id": other["id"], "channel_id": w.channel["id"], "state": "DISCOVERY"}).execute()).data[0]
    await w.tdb.table("nudge_state").insert({"conversation_id": conv2["id"], "status": "OPTED_OUT", "step": 1}).execute()
    async with client(container(w)) as c:
        assert (await c.post(f"/v1/tenants/{w.tid}/nudges/reset", headers=P)).json() == {"reset": 1}
    rows = {r["conversation_id"]: r for r in w.db.rows("nudge_state")}
    assert rows[cid]["status"] == "ACTIVE" and rows[cid]["step"] == 0 and rows[cid]["last_nudge_at"] is None and rows[conv2["id"]]["status"] == "OPTED_OUT"


async def test_events_can_be_filtered_and_the_tick_can_be_run_from_the_dashboard(re_world):
    w = re_world
    for i, (sev, ev, tid) in enumerate([("WARNING", "LLM_PROVIDER_FAILED", w.tid), ("INFO", "TURN_COMPLETED", w.tid), ("ERROR", "TURN_CRASHED", None)]):
        await w.pdb.table("events").insert({"severity": sev, "event": ev, "tenant_id": tid, "created_at": f"2026-10-0{i + 1}T10:00:00+00:00", "metadata": {}}).execute()
    async with client(container(w)) as c:
        async def get(q): return [e["event"] for e in (await c.get(f"/v1/platform/events{q}", headers=P)).json()["events"]]
        assert await get("") == ["TURN_CRASHED", "TURN_COMPLETED", "LLM_PROVIDER_FAILED"]                    # newest first
        assert await get("?severity=warning,error") == ["TURN_CRASHED", "LLM_PROVIDER_FAILED"]
        assert await get(f"?tenant_id={w.tid}") == ["TURN_COMPLETED", "LLM_PROVIDER_FAILED"]
        assert await get("?event=TURN_CRASHED&limit=1") == ["TURN_CRASHED"]
        r = (await c.post("/v1/platform/tick", headers=P)).json()
    assert r["status"] == "ok" and r["tenants"] == 1


async def test_active_config_is_readable_for_the_setup_form(re_world):
    w = re_world
    async with client(container(w)) as c:
        cfg = (await c.get(f"/v1/tenants/{w.tid}/config", headers=P)).json()["active"]
    assert cfg["version"] == 1 and cfg["config"]["identity"]["name"] == "Praise Dynasty Properties" and cfg["activated_at"]


# ── CORS ──────────────────────────────────────────────────────────────
async def test_cors_is_an_allow_list_the_dashboard_origin_passes_and_others_do_not(re_world):
    w = re_world
    c0 = container(w)
    c0.settings = Settings(_env_file=None, platform_admin_key=PLATFORM, internal_cron_secret="x", cors_allowed_origins="http://localhost:5173, https://ops.hooze.app/")
    async with client(c0) as c:
        pre = lambda origin: c.options("/v1/platform/overview", headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-api-key,ngrok-skip-browser-warning"})
        ok = await pre("http://localhost:5173")
        assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "http://localhost:5173" and "x-api-key" in ok.headers["access-control-allow-headers"].lower()
        assert (await pre("https://ops.hooze.app")).headers["access-control-allow-origin"] == "https://ops.hooze.app"
        assert "access-control-allow-origin" not in (await pre("https://evil.example")).headers
    async with client(container(w)) as c:          # default: no CORS at all
        r = await c.options("/v1/platform/overview", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in r.headers


async def test_removing_a_number_stops_its_webhook_hides_it_and_keeps_history(re_world, food_world):
    w = re_world
    async with client(container(w)) as c:
        before = (await c.get(f"/v1/platform/tenants/{w.tid}", headers=P)).json()["channels"]
        assert len(before) == 1
        assert (await c.delete(f"/v1/platform/tenants/{food_world.tid}/channels/{w.channel['id']}", headers=P)).status_code == 404       # not another client's
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}/channels/{w.channel['id']}", headers=P)).json() == {"removed": True}
        assert (await c.get(f"/v1/platform/tenants/{w.tid}", headers=P)).json()["channels"] == []
        hook = await c.post(f"/v1/webhooks/whatsapp/{w.channel['id']}", json=wa_body("hi"), headers={"x-hooze-secret": "s3cret"})
        assert hook.status_code in (403, 404)
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}/channels/{w.channel['id']}")).status_code == 401
    assert w.db.rows("channels")[0]["status"] == "disabled" and "CHANNEL_REMOVED" in w.sink.names()


# ── API key roles (migration 0006) ────────────────────────────────────
async def _role_keys(c, tid):
    out = {}
    for role in ("admin", "staff", "viewer"):
        r = await c.post(f"/v1/platform/tenants/{tid}/api-keys", json={"label": role, "role": role}, headers=P)
        assert r.status_code == 200 and r.json()["role"] == role
        out[role] = {"x-api-key": r.json()["api_key"]}
    return out


async def test_staff_key_cannot_change_settings_or_payment_keys_but_admin_can(re_world):
    w = re_world
    async with client(container(w)) as c:
        k = await _role_keys(c, w.tid)
        paystack = {"secret_key": "sk_test_x", "email_domain": "x.com"}
        for name in ("staff", "viewer"):
            assert (await c.post(f"/v1/tenants/{w.tid}/integrations/paystack", json=paystack, headers=k[name])).status_code == 403, name
            assert (await c.post(f"/v1/tenants/{w.tid}/config/activate", json={}, headers=k[name])).status_code == 403, name
            assert (await c.post(f"/v1/tenants/{w.tid}/nudges/reset", headers=k[name])).status_code == 403, name
        assert (await c.post(f"/v1/tenants/{w.tid}/integrations/paystack", json=paystack, headers=k["admin"])).status_code == 200
        assert (await c.post(f"/v1/tenants/{w.tid}/integrations/paystack", json=paystack, headers=P)).status_code == 200
    assert not any(r["kind"] == "paystack" and r.get("credentials_enc") is None for r in w.db.rows("tenant_integrations"))


async def test_staff_key_still_does_day_to_day_work_and_viewer_is_read_only(re_world):
    w = re_world
    async with client(container(w)) as c:
        k = await _role_keys(c, w.tid)
        assert (await c.get(f"/v1/tenants/{w.tid}/orders", headers=k["staff"])).status_code == 200
        assert (await c.get(f"/v1/tenants/{w.tid}/orders", headers=k["viewer"])).status_code == 200
        assert (await c.post(f"/v1/tenants/{w.tid}/config/validate", json={}, headers=k["viewer"])).status_code == 403   # any write is refused
        assert (await c.post(f"/v1/tenants/{w.tid}/nudges/reset", headers=k["admin"])).status_code == 200


async def test_keys_without_a_role_or_with_a_bad_one_never_get_more_than_staff(re_world):
    w = re_world
    key = await tenant_key(w, "legacy")                                    # no role column value => least privilege
    async with client(container(w)) as c:
        assert (await c.post(f"/v1/tenants/{w.tid}/nudges/reset", headers={"x-api-key": key})).status_code == 403
        assert (await c.get(f"/v1/tenants/{w.tid}/orders", headers={"x-api-key": key})).status_code == 200
        r = await c.post(f"/v1/platform/tenants/{w.tid}/api-keys", json={"label": "x", "role": "superuser"}, headers=P)
        assert r.status_code == 422                                          # roles are a closed list

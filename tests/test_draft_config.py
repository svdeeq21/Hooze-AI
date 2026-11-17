"""Setup drafts: business configuration can be SAVED long before WhatsApp or payments are connected.

The failure this prevents: onboarding's only save used to be "Go live", which (correctly) refuses until the channel
and (for order tenants) payments are connected — so an hour of menu entry lived in a browser tab and died with it.
A draft is a tenant_configs row (status='draft', version=0) that readiness never gates.
"""
import pytest

from tests.conftest import food_config, realestate_config
from tests.test_api import PLATFORM, client, container

P = {"x-api-key": PLATFORM}


async def _new_tenant(c):
    """A tenant fresh out of 'create client': no channel, no payments, nothing live. Returns (tenant_id, staff key)."""
    r = (await c.post("/v1/platform/tenants", headers=P, json={"slug": "newbiz", "name": "New Biz"})).json()
    return r["tenant"]["id"], r["api_key"]


async def test_a_draft_saves_with_nothing_connected_and_loads_back(food_world):
    w = food_world
    async with client(container(w)) as c:
        tid, key = await _new_tenant(c)
        H = {"x-api-key": key}
        cfg = food_config().model_dump(mode="json")

        r = await c.put(f"/v1/tenants/{tid}/config/draft", headers=H, json=cfg)
        assert r.status_code == 200
        body = r.json()
        assert body["saved"] and body["saved_at"]
        # the report tells the truth (not ready: no channel, no payments) but that never blocks SAVING
        codes = {i["code"] for i in body["report"]["blocking"]}
        assert "integrations.channel" in codes and "integrations.payments_connected" in codes

        got = (await c.get(f"/v1/tenants/{tid}/config", headers=H)).json()
        assert got["active"] is None                                   # still nothing live
        assert got["draft"]["config"]["identity"]["name"] == "Savoury Spud"
        assert got["draft"]["saved_at"]

        # and going live still refuses, exactly as before (an admin key would get 422; this staff key can't even try)
        assert (await c.post(f"/v1/tenants/{tid}/config/activate", headers=H, json=cfg)).status_code == 403


async def test_saving_again_updates_the_one_draft_row_in_place(food_world):
    w = food_world
    async with client(container(w)) as c:
        tid, key = await _new_tenant(c)
        H = {"x-api-key": key}
        first_cfg = food_config().model_dump(mode="json")
        await c.put(f"/v1/tenants/{tid}/config/draft", headers=H, json=first_cfg)
        second = food_config().model_dump(mode="json")
        second["identity"]["description"] = "now with jollof"
        await c.put(f"/v1/tenants/{tid}/config/draft", headers=H, json=second)

        rows = [r for r in w.db.tables["tenant_configs"] if r["tenant_id"] == tid and r["status"] == "draft"]
        assert len(rows) == 1 and rows[0]["version"] == 0
        got = (await c.get(f"/v1/tenants/{tid}/config", headers=H)).json()
        assert got["draft"]["config"]["identity"]["description"] == "now with jollof"


async def test_a_viewer_key_can_read_but_never_save_or_discard_a_draft(food_world):
    w = food_world
    async with client(container(w)) as c:
        tid, _ = await _new_tenant(c)
        made = (await c.post(f"/v1/platform/tenants/{tid}/api-keys", headers=P, json={"label": "ro", "role": "viewer"})).json()
        H = {"x-api-key": made["api_key"]}
        assert (await c.put(f"/v1/tenants/{tid}/config/draft", headers=H, json=food_config().model_dump(mode="json"))).status_code == 403
        assert (await c.delete(f"/v1/tenants/{tid}/config/draft", headers=H)).status_code == 403
        assert (await c.get(f"/v1/tenants/{tid}/config", headers=H)).status_code == 200


async def test_a_mangled_config_is_repaired_and_saved_rather_than_lost(food_world):
    """Drafts are a safety net, so they accept anything dict-shaped: the normalizer rebuilds broken fields from
    defaults (same as the Setup form's paste path). Losing a save over a wrong field type would defeat the point."""
    w = food_world
    async with client(container(w)) as c:
        tid, key = await _new_tenant(c)
        r = await c.put(f"/v1/tenants/{tid}/config/draft", headers={"x-api-key": key},
                        json={"identity": 5, "faqs": [{"question": "Hours?", "answer": "9-6"}]})
        assert r.status_code == 200 and r.json()["saved"]
        got = (await c.get(f"/v1/tenants/{tid}/config", headers={"x-api-key": key})).json()
        assert got["draft"]["config"]["faqs"] == [{"question": "Hours?", "answer": "9-6"}]      # the good part survived


async def test_going_live_deletes_the_draft_so_it_cannot_shadow_the_new_version(re_world):
    w = re_world
    async with client(container(w)) as c:
        cfg = realestate_config().model_dump(mode="json")
        assert (await c.put(f"/v1/tenants/{w.tid}/config/draft", headers=P, json=cfg)).status_code == 200
        r = await c.post(f"/v1/tenants/{w.tid}/config/activate", headers=P, json=cfg)
        assert r.status_code == 200 and r.json()["version"] == 2
        got = (await c.get(f"/v1/tenants/{w.tid}/config", headers=P)).json()
        assert got["draft"] is None and got["active"]["version"] == 2


async def test_discarding_a_draft_removes_it_and_discarding_again_is_a_noop(food_world):
    w = food_world
    async with client(container(w)) as c:
        tid, key = await _new_tenant(c)
        H = {"x-api-key": key}
        await c.put(f"/v1/tenants/{tid}/config/draft", headers=H, json=food_config().model_dump(mode="json"))
        assert (await c.delete(f"/v1/tenants/{tid}/config/draft", headers=H)).json()["discarded"] is True
        assert (await c.get(f"/v1/tenants/{tid}/config", headers=H)).json()["draft"] is None
        assert (await c.delete(f"/v1/tenants/{tid}/config/draft", headers=H)).json()["discarded"] is False


async def test_one_tenants_key_cannot_write_another_tenants_draft(re_world):
    a = re_world
    async with client(container(a)) as c:
        tid, key = await _new_tenant(c)                               # a key for a THIRD tenant
        assert (await c.put(f"/v1/tenants/{a.tid}/config/draft", headers={"x-api-key": key},
                            json=realestate_config().model_dump(mode="json"))).status_code == 403

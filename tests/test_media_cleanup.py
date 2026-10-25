"""Uploaded files that no live menu item uses are deleted after a save (once old enough); a client's files go with the client."""
import json
from datetime import timedelta

import pytest

from hooze.media.cleanup import purge_tenant, sweep_tenant, sweep_after_save
from hooze.media.storage import MemoryMediaStorage
from tests.conftest import food_config
from tests.test_api import PLATFORM, client, container
from tests.test_operator_api import P

OLD = "2000-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def no_real_evolution(monkeypatch):
    async def ok(*a, **k): return "deleted"                 # deleting a client also deletes its Evolution instances: never touch a real server in tests
    monkeypatch.setattr("hooze.api.operator.delete_evolution_instance", ok)


def url(storage, path): return storage.public_url(path)


async def put(storage, tid, name, *, old=True):
    path = f"{tid}/{name}"
    await storage.put(path, b"x", "image/jpeg")
    if old:
        storage.created[path] = OLD
    return path


async def world_with_menu_photos(food_world, keep=("keep.jpg",)):
    """A live menu whose first product shows the given photos."""
    w = food_world
    st = MemoryMediaStorage()
    bc = food_config()
    bc.offerings[0].media = [url(st, f"{w.tid}/{n}") for n in keep]
    from hooze.config_engine.activation import activate
    assert (await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)).ok
    return w, st


async def sweep(w, st, **kw):
    return await sweep_tenant(w.pdb, st, w.tid, now=w.clock(), **kw)


async def test_unused_old_files_are_deleted_and_files_a_live_item_uses_are_kept(food_world):
    w, st = await world_with_menu_photos(food_world, keep=("keep.jpg",))
    keep, gone = await put(st, w.tid, "keep.jpg"), await put(st, w.tid, "old-unused.jpg")
    r = await sweep(w, st)
    assert r["deleted"] == 1 and r["in_use"] == 1 and set(st.files) == {keep} and gone not in st.files


async def test_files_younger_than_the_age_gate_are_never_deleted(food_world):
    w, st = await world_with_menu_photos(food_world)
    fresh = await put(st, w.tid, "just-uploaded.jpg", old=False)
    st.created[fresh] = (w.clock() - timedelta(hours=2)).isoformat()
    r = await sweep(w, st)
    assert r["deleted"] == 0 and r["too_new"] == 1 and fresh in st.files
    st.created[fresh] = (w.clock() - timedelta(hours=25)).isoformat()                     # a day later it is fair game
    assert (await sweep(w, st))["deleted"] == 1 and fresh not in st.files


async def test_a_file_with_an_unknown_age_is_treated_as_young(food_world):
    w, st = await world_with_menu_photos(food_world)
    p = await put(st, w.tid, "mystery.jpg"); st.created[p] = None
    assert (await sweep(w, st))["deleted"] == 0 and p in st.files


async def test_removing_a_menu_item_frees_its_photos_on_the_next_sweep(food_world):
    w, st = await world_with_menu_photos(food_world, keep=("box.jpg",))
    box = await put(st, w.tid, "box.jpg")
    assert (await sweep(w, st))["deleted"] == 0 and box in st.files                       # still on the menu
    from hooze.config_engine.activation import activate
    bc = food_config(); bc.offerings = [o for o in bc.offerings if o.name != "Loaded Potato Box"]
    assert (await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)).ok                # item removed from the menu
    assert (await sweep(w, st))["deleted"] == 1 and box not in st.files


async def test_a_dry_run_reports_but_deletes_nothing(food_world):
    w, st = await world_with_menu_photos(food_world)
    gone = await put(st, w.tid, "unused.jpg")
    r = await sweep(w, st, dry_run=True)
    assert r["deleted"] == 0 and r["would_delete"] == [gone] and gone in st.files


async def test_it_never_touches_another_clients_folder(food_world, re_world):
    w, st = await world_with_menu_photos(food_world)
    other = await put(st, re_world.tid, "theirs.jpg")
    mine = await put(st, w.tid, "unused.jpg")
    await sweep(w, st)
    assert other in st.files and mine not in st.files


async def test_with_no_active_config_it_refuses_instead_of_deleting_everything(food_world):
    w, st = await world_with_menu_photos(food_world)
    p = await put(st, w.tid, "unused.jpg")
    for r in w.db.rows("tenant_configs"):
        r["status"] = "archived"
    r = await sweep(w, st)
    assert r["deleted"] == 0 and "no active configuration" in r["skipped"] and p in st.files


async def test_a_non_uuid_tenant_id_is_refused(food_world):
    w, st = await world_with_menu_photos(food_world)
    with pytest.raises(ValueError):
        await sweep_tenant(w.pdb, st, "../other", now=w.clock())


async def test_a_storage_failure_after_a_save_is_logged_and_never_raised(food_world):
    w, st = await world_with_menu_photos(food_world)
    async def boom(prefix): raise RuntimeError("storage down")
    st.list_files = boom
    c = container(w); c.media = st
    await sweep_after_save(c, w.tid)
    assert "MEDIA_CLEANUP_FAILED" in w.sink.names()


async def test_saving_a_config_through_the_api_triggers_the_cleanup(food_world):
    w, st = await world_with_menu_photos(food_world)
    gone = await put(st, w.tid, "unused.jpg")
    c_ = container(w); c_.media = st
    async with client(c_) as c:
        assert (await c.post(f"/v1/tenants/{w.tid}/integrations/paystack", json={"secret_key": "sk_test_x", "email_domain": "x.com"}, headers=P)).status_code == 200
        r = await c.post(f"/v1/tenants/{w.tid}/config/activate", json=json.loads(food_config().model_dump_json()), headers=P)
    assert r.status_code == 200, r.text
    assert gone not in st.files


# ── deleting a client ──────────────────────────────────────────────────
async def test_deleting_a_client_removes_all_its_files_but_not_anyone_elses(food_world, re_world):
    w, st = await world_with_menu_photos(food_world)
    await put(st, w.tid, "a.jpg"); await put(st, w.tid, "b.jpg")
    theirs = await put(st, re_world.tid, "theirs.jpg")
    assert await purge_tenant(st, w.tid) == 2 and set(st.files) == {theirs}


async def test_the_delete_endpoint_demands_suspension_and_the_slug_then_removes_files_and_the_client(food_world):
    w, st = await world_with_menu_photos(food_world)
    await put(st, w.tid, "a.jpg")
    slug = next(t for t in w.db.rows("tenants") if t["id"] == w.tid)["slug"]
    c_ = container(w); c_.media = st
    async with client(c_) as c:
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}?confirm={slug}", headers=P)).status_code == 409           # not suspended yet
        await c.patch(f"/v1/platform/tenants/{w.tid}", json={"status": "suspended"}, headers=P)
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}", headers=P)).status_code == 422                            # no confirmation
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}?confirm=wrong", headers=P)).status_code == 422
        assert st.files and any(t["id"] == w.tid for t in w.db.rows("tenants"))                                            # nothing happened yet
        r = await c.delete(f"/v1/platform/tenants/{w.tid}?confirm={slug}", headers=P)
        assert r.status_code == 200 and r.json()["files_deleted"] == 1
    assert st.files == {} and not any(t["id"] == w.tid for t in w.db.rows("tenants")) and "TENANT_DELETED" in w.sink.names()


async def test_if_the_files_cannot_be_deleted_the_client_is_kept(food_world):
    w, st = await world_with_menu_photos(food_world)
    from hooze.media.storage import MediaStorageError
    async def boom(paths): raise MediaStorageError("down")
    st.delete = boom
    await put(st, w.tid, "a.jpg")
    slug = next(t for t in w.db.rows("tenants") if t["id"] == w.tid)["slug"]
    c_ = container(w); c_.media = st
    async with client(c_) as c:
        await c.patch(f"/v1/platform/tenants/{w.tid}", json={"status": "suspended"}, headers=P)
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}?confirm={slug}", headers=P)).status_code == 502
    assert any(t["id"] == w.tid for t in w.db.rows("tenants"))


async def test_a_non_platform_key_cannot_delete_a_client_or_run_cleanup(food_world):
    w, st = await world_with_menu_photos(food_world)
    c_ = container(w); c_.media = st
    from tests.test_operator_api import tenant_key
    k = {"x-api-key": await tenant_key(w)}
    async with client(c_) as c:
        assert (await c.delete(f"/v1/platform/tenants/{w.tid}?confirm=x", headers=k)).status_code == 403
        assert (await c.post(f"/v1/platform/tenants/{w.tid}/media/cleanup", headers=k)).status_code == 403


async def test_deleting_a_client_also_removes_its_evolution_instances_and_reports_ones_it_could_not(food_world, monkeypatch):
    w, st = await world_with_menu_photos(food_world)
    ch2 = (await w.pdb.table("channels").insert({"tenant_id": w.tid, "kind": "whatsapp_evolution", "instance_name": "second-number", "config": {"base_url": "http://evo.test"},
                                                 "secret_hash": "0" * 64, "status": "active"}).execute()).data[0]
    seen = []
    async def fake_delete(base_url, api_key, name, http_factory=None):
        seen.append(name)
        if name == "second-number":
            raise RuntimeError("Evolution unreachable")
        return "deleted"
    monkeypatch.setattr("hooze.api.operator.delete_evolution_instance", fake_delete)
    slug = next(t for t in w.db.rows("tenants") if t["id"] == w.tid)["slug"]
    c_ = container(w); c_.media = st
    async with client(c_) as c:
        await c.patch(f"/v1/platform/tenants/{w.tid}", json={"status": "suspended"}, headers=P)
        r = await c.delete(f"/v1/platform/tenants/{w.tid}?confirm={slug}", headers=P)
    body = r.json()
    assert r.status_code == 200 and sorted(seen) == sorted([w.channel["instance_name"], "second-number"])
    assert body["instances_deleted"] == 1 and body["instances_to_remove_by_hand"][0]["instance"] == "second-number"      # reported, not blocking
    assert not any(t["id"] == w.tid for t in w.db.rows("tenants"))

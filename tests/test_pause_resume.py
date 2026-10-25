"""Pause/resume write two rows. A failure between them must never leave a customer "resumed" but still ignored."""
import pytest

from hooze.capabilities.handoff import set_ai_paused
from tests.conftest import STAFF
from tests.test_money_paths import fail_once
from tests.test_operator_api import P, tenant_key
from tests.test_api import client, container


async def chat(w):
    await w.send("hello")
    return w.db.rows("contacts")[0], w.db.rows("conversations")[0]


def paused_for_the_pipeline(w) -> bool:                    # exactly what runtime/pipeline.py checks
    return bool(w.db.rows("contacts")[0].get("ai_paused")) or w.db.rows("conversations")[0].get("status") == "handoff"


async def test_pause_then_resume_flips_both_rows(re_world):
    w = re_world
    contact, conv = await chat(w)
    await set_ai_paused(w.tdb, contact["id"], True, conversation_id=conv["id"])
    assert w.db.rows("contacts")[0]["ai_paused"] is True and w.db.rows("conversations")[0]["status"] == "handoff"
    await set_ai_paused(w.tdb, contact["id"], False, conversation_id=conv["id"])
    assert not paused_for_the_pipeline(w) and w.db.rows("conversations")[0]["status"] == "active"


@pytest.mark.parametrize("table", ["contacts", "conversations"])
async def test_a_failed_pause_write_is_reported_and_a_repeat_completes_it(re_world, table):
    w = re_world
    contact, conv = await chat(w)
    with fail_once(table, "update"):
        with pytest.raises(RuntimeError):
            await set_ai_paused(w.tdb, contact["id"], True, conversation_id=conv["id"])
    await set_ai_paused(w.tdb, contact["id"], True, conversation_id=conv["id"])
    assert w.db.rows("contacts")[0]["ai_paused"] is True and w.db.rows("conversations")[0]["status"] == "handoff"


@pytest.mark.parametrize("table", ["contacts", "conversations"])
async def test_a_failed_resume_never_leaves_the_dashboard_showing_resumed_for_a_chat_that_is_still_silenced(re_world, table):
    w = re_world
    contact, conv = await chat(w)
    await set_ai_paused(w.tdb, contact["id"], True, conversation_id=conv["id"])
    with fail_once(table, "update"):
        with pytest.raises(RuntimeError):
            await set_ai_paused(w.tdb, contact["id"], False, conversation_id=conv["id"])
    assert paused_for_the_pipeline(w)
    assert w.db.rows("contacts")[0]["ai_paused"] is True                           # and the dashboard still shows it as paused (never "resumed" while silenced)
    await set_ai_paused(w.tdb, contact["id"], False, conversation_id=conv["id"])   # repeating finishes it
    assert not paused_for_the_pipeline(w)


async def test_the_staff_resume_command_says_so_when_it_failed_instead_of_claiming_success(re_world):
    w = re_world
    contact, conv = await chat(w)
    await w.send(f"pause {contact['phone']}", phone=STAFF)
    assert paused_for_the_pipeline(w)
    with fail_once("contacts", "update"):
        await w.send(f"resume {contact['phone']}", phone=STAFF)
    assert "couldn't resume" in w.last_reply(STAFF) and "HANDOFF_TOGGLE_FAILED" in w.sink.names() and paused_for_the_pipeline(w)
    await w.send(f"resume {contact['phone']}", phone=STAFF)
    assert "Resumed" in w.last_reply(STAFF) and not paused_for_the_pipeline(w)


async def test_the_dashboard_endpoint_returns_an_error_when_it_failed_and_works_on_retry(re_world):
    w = re_world
    contact, conv = await chat(w)
    async with client(container(w)) as c:
        url = f"/v1/tenants/{w.tid}/conversations/{conv['id']}/pause"
        with fail_once("conversations", "update"):
            r = await c.post(url, headers=P)
        assert r.status_code == 500 and "try again" in r.text and "HANDOFF_TOGGLE_FAILED" in w.sink.names()
        assert (await c.post(url, headers=P)).json() == {"status": "handoff"}
        assert (await c.post(url.replace("/pause", "/resume"), headers=P)).json() == {"status": "active"}
    assert not paused_for_the_pipeline(w)

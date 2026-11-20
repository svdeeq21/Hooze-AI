"""templates/demo/*.json: one ready-made business per vertical, for sales demos. Each must load without repairs, pass the go-live
check once WhatsApp (and Paystack, for order businesses) are connected, and answer a first customer message with its welcome."""
import json
import pathlib

import pytest

from hooze.config_engine.normalize import parse_config
from hooze.config_engine.validator import validate
from tests.conftest import make_world

TEMPLATES = sorted((pathlib.Path(__file__).resolve().parents[1] / "templates" / "demo").glob("*.json"))


def test_there_is_a_template_for_every_vertical_we_pitch():
    assert {p.stem for p in TEMPLATES} >= {"restaurant", "real_estate", "clinic", "salon", "school", "retail"}


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.stem)
async def test_template_loads_cleanly_goes_live_and_greets(path):
    bc, fixes = parse_config(json.loads(path.read_text(encoding="utf-8")))
    assert fixes == [], f"{path.name} needed automatic repairs: {fixes}"
    blocking = {i.code for i in validate(bc).blocking}
    allowed = {"integrations.channel", "integrations.payments_connected"}          # connected per client, never in a template
    assert blocking <= allowed, f"{path.name} is missing content: {blocking - allowed}"
    assert not bc.handoff.staff_phones[0].startswith("234801"), "demo staff numbers must be obvious placeholders"

    bc.integrations.channel_connected = True
    bc.integrations.payments_connected = bc.sales_process.goal == "order"
    w = await make_world(bc, tenant_name=path.stem.replace("_", "-"))
    w.llm.say("Here to help!")
    await w.send("Hello")
    assert w.replies()[0] == bc.communication.greeting


async def test_demo_tenant_script_creates_a_renamed_client_with_a_draft_and_never_goes_live(re_world):
    from scripts.demo_tenant import create_demo_tenant
    from tests.test_api import PLATFORM, client, container
    w = re_world
    async with client(container(w)) as http:
        out = await create_demo_tenant(http, PLATFORM, "restaurant", name="Chops & Grill", staff_phone="08012345678")
        cfg = (await http.get(f"/v1/tenants/{out['tenant_id']}/config", headers={"x-api-key": PLATFORM})).json()
    assert out["slug"] == "chops-grill-demo" and cfg["active"] is None
    draft = cfg["draft"]["config"]
    assert draft["identity"]["name"] == "Chops & Grill" and "Welcome to Chops & Grill" in draft["communication"]["greeting"]
    assert draft["handoff"]["staff_phones"] == ["08012345678"]                         # kept as typed; normalised when compiled for go-live
    from hooze.config_engine.compiler import compile_config
    from hooze.config_engine.normalize import parse_config
    assert "2348012345678" in compile_config(parse_config(draft)[0]).runtime.staff_numbers
    assert out["still_needed"] == [] and not out["live"]        # the test chat counts as a channel; nothing was activated


async def test_demo_tenant_script_can_go_live_and_hand_out_a_working_demo_link(re_world):
    from scripts.demo_tenant import create_demo_tenant
    from tests.test_api import PLATFORM, client, container
    w = re_world
    async with client(container(w)) as http:
        out = await create_demo_tenant(http, PLATFORM, "salon", name="Glam Corner", staff_phone="08012345678", go_live=True)
        assert out["live"] and out["demo_token"]
        info = (await http.get(f"/v1/demo/{out['demo_token']}")).json()
        w.llm.say("Knotless braids are ₦35,000.")
        chat = (await http.post(f"/v1/demo/{out['demo_token']}/messages", json={"session": "prospect-0001", "text": "hi, how much are knotless braids?"})).json()
    assert info["business"] == "Glam Corner" and info["live"]
    assert [r["text"] for r in chat["replies"]] == ["Hey lovely! ✨ Welcome to Glam Corner. What are we doing for you — hair, nails or make-up?", "Knotless braids are ₦35,000."]

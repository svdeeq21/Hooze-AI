"""Digital products: no pickup/delivery question, the download link is sent automatically after a verified payment and never before."""
import json
import pathlib

from hooze.config_engine.normalize import parse_config
from tests.conftest import food_config, make_world

TEMPLATE = pathlib.Path(__file__).resolve().parents[1] / "templates" / "demo" / "digital.json"
EBOOK = "Bookkeeping Made Simple (e-book)"
LINK = "https://example.com/brightnotes/download/bookkeeping-simple"


async def _shop():
    bc, _ = parse_config(json.loads(TEMPLATE.read_text(encoding="utf-8")))
    bc.integrations.channel_connected = bc.integrations.payments_connected = True
    return await make_world(bc, tenant_name="bright-notes")


async def test_a_digital_order_skips_pickup_or_delivery_and_sends_the_link_only_after_payment():
    w = await _shop()
    w.llm.say("Great choice!", ("add_product", {"product_name": EBOOK, "modifier_names": []}))
    await w.send("I want the bookkeeping e-book")
    assert "pickup or delivery" not in w.last_reply().lower() and "ready to pay" in w.last_reply()
    assert LINK not in w.llm.calls[-1]["system"] and "[digital]" in w.llm.calls[-1]["system"]      # the AI never sees the link

    w.llm.say("ok", ("checkout", {}))
    await w.send("yes, pay")
    order = w.db.rows("orders")[0]
    assert order["status"] == "PAYMENT_PENDING" and order["fulfillment_method"] == "DIGITAL"
    assert "https://pay.test/" in w.last_reply() and LINK not in " ".join(w.replies())                # not before payment

    ref = w.db.rows("payments")[0]["reference"]
    await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 750000, "currency": "NGN"})
    assert w.replies()[-1].startswith("📩 Here's your *Bookkeeping Made Simple (e-book)*") and LINK in w.replies()[-1]
    assert "Your download is in the next message" in w.replies()[-2]
    alert = w.replies("2348000000007")[-1]                                                  # the template's staff number
    assert "New paid order" in alert and "DIGITAL" in alert and "food only" not in alert


async def test_a_redelivered_payment_webhook_does_not_send_the_link_twice():
    w = await _shop()
    w.llm.say("ok", ("add_product", {"product_name": EBOOK, "modifier_names": []})); await w.send("the e-book")
    w.llm.say("ok", ("checkout", {})); await w.send("pay")
    ref = w.db.rows("payments")[0]["reference"]
    for _ in range(2):
        await w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 750000, "currency": "NGN"})
    assert sum(LINK in t for t in w.replies()) == 1


async def test_physical_items_still_ask_pickup_or_delivery():
    w = await make_world(food_config())
    w.llm.say("ok", ("add_product", {"product_name": "Chapman", "modifier_names": []}))
    await w.send("one chapman")
    assert "pickup or delivery" in w.last_reply().lower()

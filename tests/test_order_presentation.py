"""Customer-facing order messages are laid out by code (menu, item, checkout, receipt), media is sent on request,
and a tenant with several WhatsApp numbers always answers from the number the customer wrote to."""
from hooze.capabilities.order_messages import MAX_CHUNK, format_checkout, format_menu, format_receipt
from hooze.config_engine.schema import Offering
from hooze.runtime.outbox import Outbox
from tests.conftest import CUSTOMER, food_config, make_world
from tests.fakes import FakeAdapterFactory

CAT = [
    {"name": "Jollof rice", "description": "Smoky party jollof", "price_minor": 600_000, "category": "Rice", "groups": []},
    {"name": "Fried rice", "description": "", "price_minor": 300_000, "category": "Rice", "groups": []},
    {"name": "Chapman", "description": "", "price_minor": 80_000, "category": "Drinks",
     "groups": [{"name": "Size", "required": True}, {"name": "Ice", "required": False}]},
]
ITEMS = [{"quantity": 1, "product_name": "Jollof rice", "modifiers": [], "line_total_minor": 600_000},
         {"quantity": 2, "product_name": "Chapman", "modifiers": [{"name": "Large"}], "line_total_minor": 200_000}]


# ── pure layout ────────────────────────────────────────────────────────
def test_menu_is_sectioned_by_category_one_item_per_line_with_real_prices():
    (msg,) = format_menu("7 Trays", CAT, "NGN")
    assert msg.startswith("📋 *7 Trays — Menu*")
    assert "*RICE*" in msg and "*DRINKS*" in msg and msg.index("*RICE*") < msg.index("*DRINKS*")
    assert "▫️ *Jollof rice* — ₦6,000\n    _Smoky party jollof_" in msg
    assert "✏️ _You'll choose: Size_" in msg and "Ice" not in msg          # only REQUIRED choices are flagged
    assert msg.endswith("I'll add it to your order.")


def test_menu_without_categories_is_a_single_list_and_empty_menu_is_honest():
    (msg,) = format_menu("X", [{**p, "category": ""} for p in CAT], "NGN")
    assert "*RICE*" not in msg and msg.count("▫️") == 3
    assert "don't have anything listed" in format_menu("X", [], "NGN")[0]


def test_a_long_menu_is_split_on_section_boundaries_under_the_whatsapp_limit():
    big = [{"name": f"Dish {i}", "description": "d" * 80, "price_minor": 100_00, "category": f"Cat {i // 5}", "groups": []} for i in range(80)]
    chunks = format_menu("Big", big, "NGN")
    assert len(chunks) > 1 and all(len(c) <= MAX_CHUNK + 200 for c in chunks)
    assert chunks[-1].endswith("I'll add it to your order.") and "Dish 79" in chunks[-1]
    assert sum(c.count("▫️") for c in chunks) == 80                          # nothing lost or duplicated


def test_checkout_and_receipt_are_itemised_with_modifiers_and_totals():
    c = format_checkout(ITEMS, 800_000, "DELIVERY", "5 Wuse", "https://pay/x", "NGN")
    assert "• 2× Chapman (Large) — ₦2,000" in c and "*Total: ₦8,000*" in c and "🚴 Delivery to: 5 Wuse" in c
    assert "https://pay/x" in c and "food only" in c
    r = format_receipt("7 Trays", ITEMS, 800_000, "PICKUP", "NGN")
    assert r.startswith("✅ *Payment received — thank you!*") and "*Paid: ₦8,000*" in r and "ready for pickup" in r and "7 Trays" in r


# ── through the real pipeline ──────────────────────────────────────────
async def test_show_menu_replaces_the_models_own_text_with_the_formatted_menu():
    w = await make_world(food_config())
    w.llm.say("we have some stuff", ("show_menu", {}))
    await w.send("whats the menu")
    reply = w.last_reply()
    assert "📋 *Savoury Spud — Menu*" in reply and "▫️ *Chapman* — ₦800" in reply and "we have some stuff" not in reply


async def test_show_item_sends_photos_and_video_then_asks_to_add():
    bc = food_config()
    bc.offerings[1] = Offering(name="Chapman", description="Classic", price_minor=800_00, category="Drinks",
                               media=["https://cdn/x/chapman.jpg", "https://cdn/x/pour.mp4?v=2"])
    w = await make_world(bc)
    row = next(p for p in w.db.rows("products") if p["name"] == "Chapman")
    assert row["category"] == "Drinks" and [m["type"] for m in row["media"]] == ["image", "video"]   # compiled + stored
    w.llm.say("here", ("show_item", {"product_name": "chapman"}))
    await w.send("show me the chapman")
    assert [(u, t) for _, u, t in w.adapter.media] == [("https://cdn/x/chapman.jpg", "image"), ("https://cdn/x/pour.mp4?v=2", "video")]
    assert "add *Chapman* to your order" in w.last_reply()


async def test_show_item_without_media_falls_back_to_text_and_unknown_item_offers_the_menu():
    w = await make_world(food_config())
    w.llm.say("x", ("show_item", {"product_name": "Chapman"}))
    await w.send("tell me about chapman")
    assert "*Chapman* — ₦800" in w.last_reply() and not w.adapter.media
    w.llm.say("x", ("show_item", {"product_name": "Pizza"}))
    await w.send("show me pizza")
    assert "couldn't find" in w.last_reply() and "full menu" in w.last_reply()


# ── several numbers, one business ──────────────────────────────────────
async def test_outbox_answers_from_the_number_the_conversation_is_on_and_survives_a_disabled_one():
    w = await make_world(food_config())
    ch2 = (await w.pdb.table("channels").insert({"tenant_id": w.tid, "kind": "whatsapp_evolution", "instance_name": "second", "config": {},
                                                  "secret_hash": "0" * 64, "status": "active"}).execute()).data[0]
    db = w.pdb.for_tenant(w.tid)
    contact = (await db.table("contacts").insert({"phone": "2348000000001"}).execute()).data[0]
    conv = (await db.table("conversations").insert({"contact_id": contact["id"], "channel_id": ch2["id"], "state": "BROWSING", "status": "active"}).execute()).data[0]

    class Rec(FakeAdapterFactory):
        used: list = []
        def for_channel(self, channel): self.used.append(channel["id"]); return self.adapter
    rec = Rec(w.adapter); rec.used = []
    box = Outbox(w.pdb, rec, w.tid, [], w.events)
    await box.send(contact["phone"], "hi", conversation_id=conv["id"], record=False)
    assert rec.used == [ch2["id"]]                                           # NOT the first active channel
    await w.pdb.table("channels").update({"status": "disabled"}).eq("id", ch2["id"]).execute()
    await box.send(contact["phone"], "hi again", conversation_id=conv["id"], record=False)
    assert rec.used[-1] == w.channel["id"]                                   # that number is gone: fall back, don't drop the message


async def test_activation_still_works_before_migration_0005_is_run():
    """No category/media columns yet: the product must still be saved (decoration never blocks going live)."""
    from hooze.config_engine.activation import _write

    class Res:
        def __init__(self, data): self.data = data
    class Q:
        def __init__(self, data): self.data = data
        def eq(self, *_): return self
        async def execute(self):
            if "category" in self.data or "media" in self.data:
                raise Exception('column "category" of relation "products" does not exist')
            return Res([self.data])
    class T:
        def insert(self, d): return Q(d)
        def update(self, d): return Q(d)
    class DB:
        def table(self, _): return T()
    res = await _write(DB(), "insert", {"name": "A"}, {"category": "Rice", "media": []})
    assert res.data == [{"name": "A"}]
    res = await _write(DB(), "update", {"name": "A"}, {"category": "", "media": []}, "id1")      # nothing to decorate: plain write
    assert res.data == [{"name": "A"}]                    # even with nothing to decorate, a missing column must not block activation

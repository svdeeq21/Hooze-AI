"""Re-activating a config must make the live menu EQUAL the config: price/option changes apply, removed items stop being orderable,
and the owner's sold-out toggles survive. Checkout must never charge for an item that is no longer sold."""
from hooze.config_engine.activation import _seed_catalog, activate
from hooze.config_engine.schema import OptionChoice
from tests.conftest import STAFF, food_config
from tests.test_ordering import BOX, add, order, ready_cart


def mods(w, group=None):
    return {m["name"]: m for m in w.db.rows("modifiers")}


async def reactivate(w, bc):
    res = await activate(w.pdb, w.llm, w.tid, bc, configs=w.configs)
    assert res.ok, res.report.to_dict()
    return res.seeded["catalog"]


async def test_price_description_and_option_price_changes_apply_on_reactivation(food_world):
    w = food_world
    bc = food_config()
    bc.offerings[0].price_minor = 4_000_00
    bc.offerings[0].description = "Now with extra crunch"
    bc.offerings[0].option_groups[0].options[1].price_minor = 1_500_00          # Size: Large
    stats = await reactivate(w, bc)
    box = next(p for p in w.db.rows("products") if p["name"] == BOX)
    assert box["price_minor"] == 4_000_00 and box["description"] == "Now with extra crunch"
    assert mods(w)["Large"]["price_minor"] == 1_500_00
    assert stats["products_updated"] == 1 and stats["options_updated"] == 1 and stats["products_removed"] == 0


async def test_new_options_are_added_and_removed_options_and_groups_disappear(food_world):
    w = food_world
    bc = food_config()
    bc.offerings[0].option_groups[1].options.append(OptionChoice(name="Jalapeno"))      # new topping
    bc.offerings[0].option_groups[1].options = [o for o in bc.offerings[0].option_groups[1].options if o.name != "Sausage"]
    del bc.offerings[0].option_groups[2]                                                # whole Extras group dropped
    stats = await reactivate(w, bc)
    names = set(mods(w))
    assert "Jalapeno" in names and "Sausage" not in names and "Extra Cheese" not in names
    assert {g["name"] for g in w.db.rows("modifier_groups")} == {"Size", "Toppings"}
    assert stats["options_added"] == 1 and stats["options_removed"] == 2


async def test_owner_sold_out_toggles_survive_a_sync_that_changes_other_things(food_world):
    w = food_world
    await w.send("bacon is out", phone=STAFF)
    await w.send("chapman sold out", phone=STAFF)
    bc = food_config()
    bc.offerings[0].option_groups[0].options[1].price_minor = 1_200_00
    bc.offerings[1].price_minor = 900_00
    await reactivate(w, bc)
    assert mods(w)["Bacon"]["available"] is False
    assert next(p for p in w.db.rows("products") if p["name"] == "Chapman")["available"] is False
    assert next(p for p in w.db.rows("products") if p["name"] == "Chapman")["price_minor"] == 900_00


async def test_an_unchanged_config_changes_nothing(food_world):
    w = food_world
    before = {t: [dict(r) for r in w.db.rows(t)] for t in ("products", "modifier_groups", "modifiers")}
    stats = await reactivate(w, food_config())
    assert {k: v for k, v in stats.items()} == {"products_created": 0, "products_updated": 0, "products_removed": 0, "options_added": 0, "options_updated": 0, "options_removed": 0}
    assert {t: [dict(r) for r in w.db.rows(t)] for t in before} == before


async def test_a_product_removed_from_the_config_can_no_longer_be_ordered_or_listed(food_world):
    w = food_world
    bc = food_config()
    bc.offerings = [o for o in bc.offerings if o.name != "Chapman"]
    stats = await reactivate(w, bc)
    assert stats["products_removed"] == 1 and [p["name"] for p in w.db.rows("products")] == [BOX]
    await add(w, "Chapman", qty=1)
    assert "couldn't match" in w.last_reply() and order(w) == []                         # not orderable, nothing written
    w.llm.say("x", ("show_menu", {})); await w.send("menu")
    assert "Chapman" not in w.last_reply() and BOX in w.last_reply()


async def test_removing_a_product_also_removes_its_groups_and_options_and_keeps_order_history(food_world):
    w = food_world
    await ready_cart(w)                                                                  # 2 boxes in a cart
    bc = food_config()
    bc.offerings = [o for o in bc.offerings if o.name != BOX]
    await reactivate(w, bc)
    assert w.db.rows("modifier_groups") == [] and w.db.rows("modifiers") == []
    (line,) = w.db.rows("order_items")
    assert line["product_name"] == BOX and line["unit_price_minor"] == 3_500_00           # history is a snapshot, untouched


async def test_a_second_row_with_the_same_name_does_not_stay_orderable(food_world):
    w = food_world
    await w.tdb.table("products").insert({"name": "Chapman", "description": "dup", "price_minor": 1, "available": True, "sort_order": 9}).execute()
    stats = await reactivate(w, food_config())
    assert [p["name"] for p in w.db.rows("products")].count("Chapman") == 1 and stats["products_removed"] == 1


async def test_an_empty_catalog_never_wipes_the_live_menu(food_world):
    w = food_world
    stats = await _seed_catalog(w.tdb, [])
    assert stats["products_removed"] == 0 and len(w.db.rows("products")) == 2


# ── checkout never charges for something that is no longer sold ────────
async def test_checkout_refuses_an_item_removed_from_the_menu_after_it_was_added(food_world):
    w = food_world
    await add(w, "Chapman", qty=1)
    w.llm.say("ok", ("set_fulfillment", {"method": "PICKUP"})); await w.send("pickup")
    bc = food_config(); bc.offerings = [o for o in bc.offerings if o.name != "Chapman"]
    await reactivate(w, bc)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "isn't available any more" in w.last_reply() and "Chapman" in w.last_reply()
    assert order(w)[0]["status"] == "CART" and w.db.rows("payments") == []


async def test_checkout_refuses_an_item_that_sold_out_after_it_was_added(food_world):
    w = food_world
    await add(w, "Chapman", qty=1)
    w.llm.say("ok", ("set_fulfillment", {"method": "PICKUP"})); await w.send("pickup")
    await w.send("chapman sold out", phone=STAFF)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "isn't available any more" in w.last_reply() and w.db.rows("payments") == []


async def test_checkout_refuses_a_cart_whose_chosen_option_was_removed(food_world):
    w = food_world
    await ready_cart(w)                                                                  # Cheese + Large
    bc = food_config()
    bc.offerings[0].option_groups[0].options = [o for o in bc.offerings[0].option_groups[0].options if o.name != "Large"]
    await reactivate(w, bc)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "isn't available any more" in w.last_reply() and order(w)[0]["status"] == "CART"


async def test_checkout_still_works_when_everything_is_still_sold(food_world):
    w = food_world
    await ready_cart(w)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert order(w)[0]["status"] == "PAYMENT_PENDING" and len(w.db.rows("payments")) == 1


# ── checkout re-prices from the live menu and never charges an amount the customer has not seen ──
async def _pickup(w):
    w.llm.say("ok", ("set_fulfillment", {"method": "PICKUP"})); await w.send("pickup")


async def test_a_price_raised_after_the_item_was_added_is_shown_and_confirmed_before_any_charge(food_world):
    w = food_world
    await add(w, "Chapman", qty=2); await _pickup(w)
    old_total = order(w)[0]["subtotal_minor"]
    bc = food_config(); bc.offerings[1].price_minor = 1_200_00                          # Chapman gets dearer while the cart is open
    await reactivate(w, bc)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    reply = w.last_reply()
    assert "prices changed" in reply and "Chapman" in reply and w.db.rows("payments") == [] and order(w)[0]["status"] == "CART"
    assert order(w)[0]["subtotal_minor"] == 2 * 1_200_00 != old_total and "2,400" in reply                  # shows the new total
    w.llm.say("x", ("checkout", {})); await w.send("pay")                                                     # customer confirms by asking again
    (p,) = w.db.rows("payments")
    assert p["amount_minor"] == 2 * 1_200_00 and order(w)[0]["status"] == "PAYMENT_PENDING"


async def test_an_option_price_change_is_picked_up_at_checkout_and_a_price_drop_is_also_confirmed_first(food_world):
    w = food_world
    await ready_cart(w)                                                                  # Cheese box, Large
    before = order(w)[0]["subtotal_minor"]
    bc = food_config(); bc.offerings[0].option_groups[0].options[1].price_minor = 100_00   # Large is now cheaper
    await reactivate(w, bc)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "prices changed" in w.last_reply() and order(w)[0]["subtotal_minor"] < before and w.db.rows("payments") == []
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert w.db.rows("payments")[0]["amount_minor"] == order(w)[0]["subtotal_minor"]


async def test_nothing_changed_means_no_extra_confirmation_step(food_world):
    w = food_world
    await ready_cart(w)
    w.llm.say("x", ("checkout", {})); await w.send("pay")
    assert "prices changed" not in w.last_reply() and len(w.db.rows("payments")) == 1


async def test_a_half_built_item_for_a_removed_product_is_dropped_from_the_open_cart(food_world):
    w = food_world
    await add(w, BOX, qty=1)                                                             # box needs options -> stays a draft
    assert order(w)[0]["draft"]
    bc = food_config(); bc.offerings = [o for o in bc.offerings if o.name != BOX]
    await reactivate(w, bc)
    assert order(w)[0]["draft"] is None

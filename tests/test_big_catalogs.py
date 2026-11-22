"""Catalogs of 1,000+ items: a big menu is not sent to the AI whole (it was ~20,000 tokens per message), any item can still be ordered by
name, "show me the menu" sends sections, and listings are ranked by the customer's budget and area before meaning-similarity."""
import re

from hooze.config_engine.schema import (BookingCfg, BusinessConfig, Handoff, Identity, Integrations, Offering, OptionChoice, OptionGroup,
                                        SalesProcess, Slot)
from tests.conftest import STAFF, food_config, make_world

SECTIONS = ["Rice", "Soups", "Grills", "Pastries", "Drinks"]


def big_shop(n: int = 1000) -> BusinessConfig:
    bc = food_config()
    bc.offerings = [Offering(name=f"{SECTIONS[i % 5][:-1]} Special {i}", category=SECTIONS[i % 5], description=f"House {SECTIONS[i % 5].lower()} number {i}",
                             price_minor=2000_00 + i * 10) for i in range(n)]
    bc.offerings.append(Offering(name="Jollof Rice Deluxe", category="Rice", description="Smoky party jollof with plantain", price_minor=4500_00,
                                 option_groups=[OptionGroup(name="Protein", required=True, options=[OptionChoice(name="Chicken"), OptionChoice(name="Turkey", price_minor=1500_00)])]))
    return bc


async def test_a_1000_item_menu_sends_only_the_relevant_items_to_the_ai():
    w = await make_world(big_shop())
    w.llm.say("We have Jollof Rice Deluxe!")
    await w.send("do you have jollof rice with turkey?")
    system = w.llm.calls[-1]["system"]
    assert len(system) // 4 < 4000                                         # was ~20,000 tokens with the whole menu inline
    assert "1001 items" in system and "Rice (201)" in system and "Jollof Rice Deluxe" in system and "Turkey" in system


async def test_any_item_on_a_big_menu_can_still_be_ordered_by_name():
    w = await make_world(big_shop())
    w.llm.say("Added!", ("add_product", {"product_name": "Grill Special 702", "modifier_names": []}))
    await w.send("one Grill Special 702 please")
    assert [i["product_name"] for i in w.db.rows("order_items")] == ["Grill Special 702"]


async def test_show_menu_on_a_big_menu_sends_sections_then_one_section():
    w = await make_world(big_shop())
    w.llm.say("Here you go", ("show_menu", {}))
    await w.send("what do you have?")
    overview = w.last_reply()
    assert "1001 items in 5 sections" in overview and "• Drinks (200)" in overview and len(w.replies()) == 1
    w.llm.say("Here are the drinks", ("show_menu", {"category": "drinks"}))
    await w.send("show me the drinks")
    sent = " ".join(w.replies()[1:])
    assert "Drink Special 4" in sent and "Rice Special" not in sent


def agency(n: int = 300) -> BusinessConfig:
    areas = ["Lekki", "Ikoyi", "Ajah", "Yaba", "Jabi"]
    offers = [Offering(name=f"{areas[i % 5]} 3-Bedroom Apartment {i}", description=f"3 bedroom apartment in {areas[i % 5]}",
                       price_minor=(120 + (i * 7) % 300) * 1_000_000_00, details={"location": areas[i % 5]}) for i in range(n)]
    return BusinessConfig(identity=Identity(name="Big Agency"), offerings=offers,
                          qualification_slots=[Slot(name="location"), Slot(name="budget", type="money")],
                          sales_process=SalesProcess(goal="consult_and_book"), booking=BookingCfg(enabled=True),
                          handoff=Handoff(contact_name="Team", staff_phones=[STAFF]), integrations=Integrations(channel_connected=True))


async def test_listings_are_ranked_by_budget_and_area_and_over_budget_ones_are_labelled():
    w = await make_world(agency())
    w.llm.say("Noted", ("record_slots", {"slots": {"location": "Lekki", "budget": "200 million"}}))
    await w.send("I want a 3 bedroom apartment in Lekki, budget 200 million")
    w.llm.say("Here are some options")
    await w.send("what do you have for me?")
    system = w.llm.calls[-1]["system"]
    records = re.findall(r"\[K\d+\] ([^:]+): .*?\[([^\]]*budget[^\]]*)\]\s*$", system, re.M)
    assert records, system[system.find("RELEVANT RECORDS"):][:1500]
    first_title, first_fit = records[0]
    assert first_title.startswith("Lekki") and "in the area they asked for" in first_fit and "within their budget" in first_fit
    for title, fit in records:
        price = int(re.search(r"Apartment (\d+)", title).group(1))
        over = (120 + (price * 7) % 300) > 200 * 1.10
        assert ("ABOVE their budget" in fit) == over                           # every over-budget listing is labelled as such

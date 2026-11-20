"""Replays the real-estate demo that went wrong (Nov 2026): the customer asked for photos and details and got handed to a person; the
same budget found the Lekki listing in one chat and missed it in another; the welcome was sent twice; links and brochures were never sent."""
import re

from hooze.config_engine.schema import (BookingCfg, BusinessConfig, Communication, Handoff, Identity, Integrations, Offering, Restrictions,
                                        SalesProcess, Slot)
from hooze.runtime.models import Action, Interpretation
from tests.conftest import STAFF, make_world

IKATE = "Ikate 4-Bedroom Penthouse"


def agency() -> BusinessConfig:
    return BusinessConfig(
        identity=Identity(name="D-Agency Prime Properties", industry="real estate"),
        offerings=[
            Offering(name=IKATE, description="Newly built 4-bedroom penthouse with rooftop terrace in Ikate, Lekki, Lagos", price_minor=450_000_000_00,
                     url="https://d-agency.example/ikate-penthouse", details={"location": "Ikate, Lekki, Lagos", "price": "₦450,000,000"},
                     media=["https://cdn.example/ikate-1.jpg", "https://cdn.example/ikate-2.jpg", "https://cdn.example/ikate-tour.mp4", "https://cdn.example/ikate-brochure.pdf"]),
            Offering(name="Jahi 4-Bedroom Terrace", description="4-bedroom terrace duplex in Jahi, Abuja with 24-hour security", price_minor=300_000_000_00,
                     details={"location": "Jahi, Abuja", "price": "₦300,000,000"}),
            Offering(name="Guzape Plot", description="1,000sqm residential land in Guzape, Abuja", price_minor=150_000_000_00, details={"location": "Guzape, Abuja"}),
            Offering(name="Ajah 3-Bedroom Flat", description="3-bedroom flat for rent in Ajah, Lagos", price_minor=6_000_000_00, details={"location": "Ajah, Lagos"}),
        ],
        qualification_slots=[Slot(name="purpose", type="enum", values=["buy", "rent", "invest"]), Slot(name="location"), Slot(name="budget", type="money")],
        sales_process=SalesProcess(goal="consult_and_book"), booking=BookingCfg(enabled=True, subject_label="inspection"),
        handoff=Handoff(contact_name="D-Agency Prime Properties Sales Team", staff_phones=[STAFF]),
        communication=Communication(greeting="Welcome to D-Agency Prime Properties. Are you looking to buy, rent or invest?"),
        restrictions=Restrictions(never_say=["guaranteed returns"]), integrations=Integrations(channel_connected=True))


def ref_for(system: str, title: str) -> str:
    m = re.search(r"\[(K\d+)\] " + re.escape(title), system)
    assert m, f"{title} is not in RELEVANT RECORDS:\n{system[system.find('RELEVANT RECORDS'):][:800]}"
    return m.group(1)


def share(title: str, reply: str = "Here you go!"):
    return lambda system, prompt: Interpretation(reply=reply, intent="question", actions=[Action(function="share_item", args={"ref": ref_for(system, title)})])


async def test_the_lekki_demo_shows_the_listing_with_photos_link_and_brochure_instead_of_handing_off():
    w = await make_world(agency())
    assert (await w.send("Hello Good evening")).status == "greeted"                       # one welcome, no second greeting
    assert w.replies() == ["Welcome to D-Agency Prime Properties. Are you looking to buy, rent or invest?"]

    w.llm.say("Great! Which location?", ("record_slots", {"slots": {"purpose": "invest"}}))
    await w.send("I'm looking for investment opportunities")
    w.llm.say("Lekki is a great choice. What's your budget?", ("record_slots", {"slots": {"location": "Lekki"}}))
    await w.send("Lekki")
    w.llm.say(f"The {IKATE} in Ikate, Lekki is ₦450m and fits. Would you like to see the details and photos?", ("record_slots", {"slots": {"budget": "500 million"}}))
    await w.send("500 million should do")
    # "500 million" says nothing about Lekki; the search now also uses what the customer already told us
    assert re.search(r"\[K\d+\] " + IKATE, w.llm.calls[-1]["system"])
    assert "2 photos, 1 video, 1 document on file" in w.llm.calls[-1]["system"]

    w.llm.script.append(share(IKATE))
    out = await w.send("Yes please")                                                     # matches no listing by itself
    assert out.status == "replied" and not w.db.rows("contacts")[0]["ai_paused"]
    card = next(t for t in w.replies() if t.startswith(f"*{IKATE}*"))
    assert "₦450,000,000" in card and "🔗 https://d-agency.example/ikate-penthouse" in card
    assert [(m[2], m[1].rsplit("/", 1)[-1]) for m in w.adapter.media] == [("image", "ikate-1.jpg"), ("image", "ikate-2.jpg"), ("video", "ikate-tour.mp4"), ("document", "ikate-brochure.pdf")]

    w.clock.advance(minutes=3)                                                            # a real pause (8 replies a minute is the limit)
    w.llm.script.append(share(IKATE, "Here are the pictures again."))
    await w.send("Can I see the images again?")                                          # asked again explicitly: sent again, not refused
    assert len(w.adapter.media) == 8 and not any("Human needed" in t for t in w.replies(STAFF))


async def test_after_a_real_handoff_the_customer_gets_one_acknowledgement_not_silence():
    w = await make_world(agency())
    await w.send("I want to speak to a human")
    n = len(w.replies())
    await w.send("Can I see the property?")
    assert w.replies()[n:] == ["Thanks, I've passed that on. D-Agency Prime Properties Sales Team will reply here shortly 🙏"]
    await w.send("hello?")
    assert len(w.replies()) == n + 1


def test_the_model_is_told_not_to_hand_off_for_photos_and_not_to_repeat_itself_when_the_customer_is_unsure():
    import inspect
    from hooze.capabilities import handoff
    from hooze.runtime.prompt import PLATFORM_RULES
    assert "never repeat your question" in PLATFORM_RULES
    assert "Never to show details, photos" in inspect.getsource(handoff)

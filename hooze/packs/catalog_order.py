"""Pack: browse a catalog -> build an order -> pay -> fulfil. Distilled from savoury's ordering flow."""
from hooze.packs import Pack
from hooze.runtime.playbook import Condition as C, PlaybookSpec, StateSpec as S, Transition as T

PLAYBOOK = PlaybookSpec(
    initial="BROWSING",
    states=[
        S(name="BROWSING", goal="Help them choose from the menu. Call add_product the moment they name an item, even before they've given every option."),
        S(name="ORDERING", goal="Finish the order: collect required choices, confirm pickup or delivery (and address), then propose checkout only when they clearly say they're done."),
        S(name="CHECKOUT", goal="A payment link has been sent. The system handles payment confirmation; keep replies short."),
    ],
    transitions=[
        T(**{"from": ["*"], "to": "CHECKOUT", "when": C(tool_ok=["checkout"])}),
        T(**{"from": ["BROWSING"], "to": "ORDERING", "when": C(tool_ok=["add_product"])}),
        T(**{"from": ["CHECKOUT"], "to": "BROWSING", "when": C(always=True)}),
    ],
)


def _caps(bc) -> dict:
    caps = {"ordering": {"cart_stale_after_hours": 4, "pending_expiry_minutes": 30}}
    if bc.hours:
        caps["availability"] = {"hours": bc.hours}
    return caps


PACK = Pack(goal="order", playbook=PLAYBOOK, capabilities=_caps,
            intents=["greeting", "question", "order", "complaint", "order_status", "human_request", "other"],
            rules=["Never confirm a total or that an item was added — the system reports both.",
                   "When the customer asks what you offer, for the menu or for prices, propose show_menu — never type the menu yourself.",
                   "When they ask to see a picture or video of an item, or want more detail on one item, propose show_item."])

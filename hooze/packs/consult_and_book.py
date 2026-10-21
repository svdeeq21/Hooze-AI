"""Pack: consult -> present verified options -> book a visit/appointment/call.

Distilled from pd `conversation_state.py` + `state_prompts.py` (PD_STATE_GOALS) + `follow_up_engine`
IDLE_THRESHOLD_HOURS. State names are generic (COLD->NEW, PITCH->PRESENTING, SOFT_CLOSE->CONFIRMING,
CALL_INVITE->BOOKING, DEAD->LOST); the PD rules survive as edges:
  * "show me"/a shared record from any early state -> PRESENTING; there is NO edge back to DISCOVERY
    from PRESENTING or later (pd's "DISCOVERY never after PITCH" as graph structure).
  * a booking intent from any active state jumps to BOOKING (pd's viewing-request hard interrupt).
Real estate, dental, schools, car dealers, salons and clinics are all this pack with different data.
"""
from hooze.packs import Pack
from hooze.runtime.playbook import Condition as C, PlaybookSpec, StateSpec as S, Transition as T

ACTIVE = ["NEW", "DISCOVERY", "PRESENTING", "REFINING", "CONFIRMING", "BOOKING", "NURTURE"]

PLAYBOOK = PlaybookSpec(
    initial="NEW",
    states=[
        S(name="NEW", goal="Welcome them briefly and find out what they're looking for. One question only."),
        S(name="DISCOVERY", goal="Understand their needs through natural conversation, one question at a time. Infer instead of interrogating; record what they state with record_slots.",
          idle_after_hours=24, nudges=["Hi {name}, just checking in — are you still looking? Tell me what matters most and I'll narrow it down.", "Hi {name}, happy to keep helping whenever you're ready.", "Hi {name}, last check-in from me — message anytime if you'd like to continue."]),
        S(name="PRESENTING", goal="Present verified records that fit what they told you (propose share_item). Do not re-ask qualifying questions. Flag anything that doesn't match their stated requirements.",
          idle_after_hours=48, nudges=["Hi {name}, did you get a chance to look at what I sent? Happy to answer any questions.", "Hi {name}, want me to show you a couple of other options?", "Hi {name}, shall I keep an eye out for something closer to what you want?"]),
        S(name="REFINING", goal="They want something different. Ask what to change (one question), then present better-fitting records.", idle_after_hours=48,
          nudges=["Hi {name}, I've got a few more options that might fit better — want to see them?", "Hi {name}, still looking? Tell me what to adjust and I'll refine it.", "Hi {name}, I'm here whenever you want to continue."]),
        S(name="CONFIRMING", goal="Check alignment after presenting: does this fit? If yes, move to booking; if not, learn what to change. Do not present more before hearing back.", idle_after_hours=24,
          nudges=["Hi {name}, does that one feel right for you? I can arrange a time if you'd like.", "Hi {name}, want me to set up a time to see it?", "Hi {name}, just following up — let me know how you'd like to proceed."]),
        S(name="BOOKING", goal="They're ready. Ask which day/time suits them; once you have a specific record and a preferred time, propose request_booking.", idle_after_hours=12, escalate_after_nudges=1,
          nudges=["Hi {name}, what day works best for you? I'll lock in the request with the team."]),
        S(name="NURTURE", goal="Not ready yet. Stay light, no pressure; answer questions and keep the door open.", idle_after_hours=120,
          nudges=["Hi {name}, a quick note from us — if your plans have moved forward, I'm glad to pick up where we left off."]),
        S(name="BOOKED", terminal=True, goal="Request submitted. Answer questions; the team will confirm the slot."),
        S(name="LOST", terminal=True, goal="Customer opted out or is no longer interested. Do not pursue."),
    ],
    transitions=[
        T(**{"from": ["*"], "to": "BOOKED", "when": C(tool_ok=["request_booking"])}),
        T(**{"from": ["*"], "to": "LOST", "when": C(intent_in=["not_interested"])}),
        T(**{"from": ACTIVE, "to": "BOOKING", "when": C(intent_in=["booking"])}),
        T(**{"from": ["NEW", "DISCOVERY", "REFINING", "NURTURE"], "to": "PRESENTING", "when": C(tool_ok=["share_item"])}),
        T(**{"from": ["NEW", "DISCOVERY", "BOOKING"], "to": "NURTURE", "when": C(keywords_any=[r"\bjust (browsing|looking)\b", r"\bnot ready\b", r"\bmaybe later\b", r"\bnot now\b"])}),
        T(**{"from": ["NEW"], "to": "DISCOVERY", "when": C(always=True)}),
        T(**{"from": ["DISCOVERY"], "to": "PRESENTING", "when": C(slots_complete=True)}),
        T(**{"from": ["PRESENTING"], "to": "CONFIRMING", "when": C(always=True)}),
        T(**{"from": ["CONFIRMING"], "to": "REFINING", "when": C(keywords_any=[r"\bnot really\b", r"\bnot quite\b", r"\bsomething else\b", r"\bother options\b", r"\bnot this\b", r"\bnot for me\b"])}),
        T(**{"from": ["REFINING"], "to": "PRESENTING", "when": C(tool_ok=["share_item"])}),
        T(**{"from": ["NURTURE"], "to": "DISCOVERY", "when": C(intent_in=["question", "order"])}),
    ],
)


def _caps(bc) -> dict:
    slots = [s.model_dump() for s in bc.qualification_slots]
    seen: list[str] = []
    for o in bc.offerings:
        for k in o.details:
            if k not in seen:
                seen.append(k)
    fmt = {"location": "📍 {v}", "price": "💰 {v}"}
    card = [{"field": "title", "fmt": "*{v}*"}] + [{"field": k, "fmt": fmt.get(k, f"{k.replace('_', ' ').title()}: {{v}}")} for k in seen]
    caps = {"qualification": {"slots": slots, "stuck_after_user_messages": bc.stuck_after_user_messages},
            "knowledge": {"top_k": 5, "threshold": 0.30, "card_fields": card, "share_media": True},
            "nudges": {"enabled": True, "dry_run": True, "window": [8, 20], "max_steps": 3}}   # dry-run until reviewed on real conversations
    if bc.booking.enabled:
        caps["booking"] = {"subject_label": bc.booking.subject_label}
    return caps


PACK = Pack(goal="consult_and_book", playbook=PLAYBOOK, capabilities=_caps,
            intents=["greeting", "question", "booking", "objection", "human_request", "not_interested", "other"],
            rules=["Ground every recommendation in RELEVANT RECORDS. If the customer's requirement isn't met by a record, say so plainly and offer the closest fit as a choice.",
                   "Do not push a booking before the customer has seen a record and shown clear interest; once they do, move straight to booking."])

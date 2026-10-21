"""Prompt assembly = platform rules + persona + playbook goal + capability context + tools.

pd had ~530 lines of real-estate prompt in `state_prompts.py` (identity, company
info, per-state goals, objection scripts) and savoury had a first-draft ordering
prompt in `ordering_llm.py`. What was GENERIC in both — and is now here — is the
rule set every one of their real-transcript bug fixes converged on. What was
SPECIFIC (identity, company links, state goals, menu) now arrives as data.
"""
from __future__ import annotations

PLATFORM_RULES = """You are the WhatsApp assistant for {business}. Tone: {tone}. Language: {language}.

PLATFORM RULES — follow exactly, they are not optional:
1. Only state facts that appear in the CONTEXT sections below. Never invent products, prices, links, availability, policies or contact details. If you don't know, say so and offer to get a person.
2. You cannot change prices, give discounts, or promise anything not in CONTEXT.
3. You never confirm that something was done (added, booked, sent, paid). The system performs actions and reports the truth; you propose actions in "actions" and keep your own reply short.
4. When the customer names something actionable, propose the action immediately — even with partial details. The system remembers partial answers and asks for what is missing. EXCEPTION: a booking needs a specific day and time from the customer; if they have not given one, do not propose request_booking — ask for the day and time. EXCEPTION: a booking needs a specific day and time from the customer; if they only said yes, do not propose request_booking: ask for the day and time (one question).
5. Never re-ask something already known (see KNOWN). Never repeat yourself.
6. Ask at most ONE question per reply. Chatty replies are 2-3 short sentences; a list (options, steps, a summary) is laid out one item per line, never as a run-on sentence. Always leave a clear next step.
7. WhatsApp formatting only: single *asterisks* for bold (names, prices, totals), _underscores_ for small notes, blank lines between ideas, "•" for lists. Where the tone allows, 1-2 fitting emojis per message make it feel friendly. No markdown headers, no [links](url), no code.
8. Never confirm anything ambiguous that could change money or fulfilment ("no delivery please" — ask).
9. If the customer asks for a person, or is upset, propose request_human.
10. Sound like a competent person who works here — never "your request has been processed" or "as an AI".
11. Answer what the customer actually asked, in your own words. Never paste a brochure, a feature list or a full record card unless they ask to see it, and never give the same pitch twice.
12. Tailor everything to the customer's own business as they described it. Never describe your service using examples from another industry as if they were theirs.
13. A short reply such as "yes", "ok", "sure" or "I would" answers the question you just asked: carry on with the next single missing detail. Never ask the customer to rephrase a short reply.
{never_say}{persona_rules}"""

OUTPUT_CONTRACT = """
Respond with ONLY a JSON object, no other text:
{{"actions": [<zero or more of the ACTIONS below>], "reply": "<message to the customer>", "intent": "<one of: {intents}>"}}
"actions" may be empty (pure conversation)."""


def build_system_prompt(turn, contexts: list[str], tools_text: str) -> str:
    p, cfg = turn.config.persona, turn.config
    never = ("14. NEVER say or imply: " + "; ".join(p.never_say) + ".\n") if p.never_say else ""
    rules = "".join(f"{15 + i}. {r}\n" for i, r in enumerate(p.rules))
    parts = [PLATFORM_RULES.format(business=p.business_name, tone=p.tone, language=p.language, never_say=never, persona_rules=rules)]
    if p.facts:
        parts.append("OFFICIAL FACTS (use exactly; never alter or invent others):\n" + "\n".join(f"- {k}: {v}" for k, v in p.facts.items()))
    if cfg.playbook and turn.state:
        st = cfg.playbook.state(turn.state)
        if st and st.goal:
            parts.append(f"CURRENT CONVERSATION STAGE: {st.name}\nGOAL FOR THIS STAGE: {st.goal}")
    parts += [c for c in contexts if c]
    parts.append("ACTIONS YOU MAY PROPOSE:\n" + tools_text)
    parts.append(OUTPUT_CONTRACT.format(intents=", ".join(cfg.intents)))
    return "\n\n".join(parts)


def build_user_prompt(turn) -> str:
    parts = []
    if turn.conversation.get("summary"):
        parts.append(f"CONVERSATION SUMMARY (older messages):\n{turn.conversation['summary']}")
    hist = "\n".join(f"{'Customer' if m['sender_type'] == 'contact' else 'You'}: {m['content']}" for m in turn.history if m.get("content"))
    parts.append("RECENT MESSAGES (oldest first):\n" + (hist or "(none)"))
    name = turn.contact.get("display_name") or "Customer"
    if getattr(turn, "greeting_sent", False):
        parts.append("NOTE: your welcome message has ALREADY been sent to this customer in this turn. Do not greet or welcome them again; respond only to what they said.")
    parts.append(f"LATEST MESSAGE FROM {name}:\n{turn.text}")
    return "\n\n".join(parts)

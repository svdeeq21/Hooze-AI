"""Compiler: (BusinessConfig + pack) -> RuntimeConfig + seed data. Pure, deterministic, unit-testable.

No `if industry == ...` anywhere: the pack is chosen by `sales_process.goal`, everything else is data mapping.
"""
from __future__ import annotations
import re

from hooze.kernel.phone import normalize_phone
from dataclasses import dataclass, field

from hooze.capabilities.knowledge import KnowledgeDoc
from hooze.config_engine.schema import BusinessConfig
from hooze.packs import get_pack
from hooze.payments.base import fmt_money
from hooze.runtime.config import Persona, RuntimeConfig


@dataclass
class Compiled:
    runtime: RuntimeConfig
    knowledge_docs: list[KnowledgeDoc] = field(default_factory=list)
    catalog: list[dict] = field(default_factory=list)      # products + groups + options, for the ordering capability


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _hours_text(hours: dict) -> str:
    if not hours:
        return ""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out = []
    for i, d in enumerate(days):
        w = hours.get(str(i), hours.get("default"))
        out.append(f"{d} {w[0]}-{w[1]}" if w else f"{d} closed")
    return ", ".join(out)


def compile_config(bc: BusinessConfig, version: int = 1) -> Compiled:
    pack = get_pack(bc.sales_process.goal)
    idt = bc.identity
    facts = {**({"Website": idt.website} if idt.website else {}), **({"Address": idt.address} if idt.address else {}), **idt.facts}
    if bc.hours:
        facts["Opening hours"] = _hours_text(bc.hours)

    caps = pack.capabilities(bc)
    caps["handoff"] = {"contact_name": bc.handoff.contact_name, "human_request_patterns": bc.handoff.human_request_patterns}

    runtime = RuntimeConfig(
        version=version,
        persona=Persona(business_name=idt.name, tone=bc.communication.tone, language=", ".join(idt.languages), facts=facts,
                        rules=[*pack.rules, *bc.communication.rules], never_say=[*bc.restrictions.never_say, *bc.restrictions.prohibited_claims]),
        timezone=idt.timezone, currency=idt.currency, capabilities=caps, playbook=pack.playbook, intents=pack.intents,
        staff_numbers=[n for n in (normalize_phone(p) for p in bc.handoff.staff_phones) if n],
        first_contact_message=bc.communication.greeting or None,
    )

    docs: list[KnowledgeDoc] = []
    catalog: list[dict] = []
    for o in bc.offerings:
        price = fmt_money(o.price_minor, idt.currency) if o.price_minor is not None else (o.price_note or "Price on request")
        if bc.sales_process.goal == "order":
            catalog.append({"name": o.name, "description": o.description, "price_minor": o.price_minor or 0, "category": o.category,
                            "media": [{"type": "video" if u.lower().split("?")[0].endswith((".mp4", ".mov", ".3gp")) else "image", "url": u} for u in o.media],
                            "groups": [{"name": g.name, "selection": g.selection, "required": g.required, "max_selections": g.max_included,
                                        "options": [{"name": c.name, "price_minor": c.price_minor} for c in g.options]} for g in o.option_groups]})
        else:
            data = {"price": price, **o.details, **({"url": o.url} if o.url else {})}
            body = " ".join(filter(None, [o.description, *(f"{k}: {v}" for k, v in o.details.items()), f"Price: {price}"]))
            docs.append(KnowledgeDoc(external_id=f"offering:{_slug(o.name)}", kind="offering", title=o.name, content=body, data=data,
                                     media=[{"type": "video" if u.lower().endswith(".mp4") else "image", "url": u} for u in o.media]))
    for f in bc.faqs:
        docs.append(KnowledgeDoc(external_id=f"faq:{_slug(f.question)}", kind="faq", title=f.question, content=f.answer))
    return Compiled(runtime=runtime, knowledge_docs=docs, catalog=catalog)

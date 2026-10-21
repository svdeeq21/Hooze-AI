"""Readiness validation: the gate between "onboarding produced a config" and "it can talk to customers".

Implements the checklist from the Hooze SaaS design (is pricing defined? are escalation rules defined? are business
hours known? are qualification criteria complete? are prohibited claims defined? is booking logic complete? are
required integrations connected?) as executable rules. `blocking` issues prevent activation; `warnings` don't.
This is what lets onboarding be automated: an incomplete business cannot go live by accident, and the failing
checks are exactly the questions the AI interview / dashboard should ask next.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from hooze.config_engine.schema import BusinessConfig


@dataclass
class Issue:
    code: str
    message: str
    field: str = ""


@dataclass
class ValidationReport:
    blocking: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    checks_total: int = 0
    checks_passed: int = 0

    @property
    def ok(self) -> bool:
        return not self.blocking

    @property
    def completeness(self) -> float:
        return round(self.checks_passed / self.checks_total, 2) if self.checks_total else 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "completeness": self.completeness,
                "blocking": [i.__dict__ for i in self.blocking], "warnings": [i.__dict__ for i in self.warnings]}


def validate(bc: BusinessConfig) -> ValidationReport:
    r = ValidationReport()

    def check(passed: bool, code: str, msg: str, fld: str = "", blocking: bool = True) -> None:
        r.checks_total += 1
        if passed:
            r.checks_passed += 1
        else:
            (r.blocking if blocking else r.warnings).append(Issue(code, msg, fld))

    goal = bc.sales_process.goal
    check(bool(bc.identity.name.strip()), "identity.name", "Business name is required.", "identity.name")
    check(goal is not None, "sales_process.goal", "Choose what customers should be able to do: order, or consult & book.", "sales_process.goal")
    check(bool(bc.handoff.staff_phones), "handoff.staff_phones", "At least one staff WhatsApp number is required so the assistant can escalate to a human.", "handoff.staff_phones")
    check(bool(bc.handoff.contact_name.strip()), "handoff.contact_name", "Name the person customers will be handed to.", "handoff.contact_name", blocking=False)
    check(bool(bc.restrictions.never_say or bc.restrictions.prohibited_claims), "restrictions", "No prohibited claims defined (e.g. guarantees, discounts, legal/medical promises).", "restrictions", blocking=False)
    check(bool(bc.identity.facts or bc.identity.website), "identity.facts", "No official links/contacts provided; the assistant will refuse to quote any.", "identity.facts", blocking=False)

    if goal == "order":
        check(bool(bc.offerings), "offerings", "Add at least one product to the menu.", "offerings")
        unpriced = [o.name for o in bc.offerings if o.price_minor is None]
        check(not unpriced, "offerings.price", f"Every product needs a price: {', '.join(unpriced[:5])}", "offerings.price")
        empty = [f"{o.name}/{g.name}" for o in bc.offerings for g in o.option_groups if g.required and not g.options]
        check(not empty, "offerings.options", f"Required option groups have no options: {', '.join(empty[:5])}", "offerings.option_groups")
        check(bool(bc.integrations.payments), "integrations.payments", "Choose a payment provider.", "integrations.payments")
        check(bc.integrations.payments_connected, "integrations.payments_connected", "Payment provider credentials are not connected yet.", "integrations.payments_connected")
        check(bool(bc.hours), "hours", "Opening hours not set; the assistant will accept orders 24/7.", "hours", blocking=False)
    elif goal == "consult_and_book":
        check(bool(bc.qualification_slots), "qualification_slots", "Define what you need to know before recommending (e.g. budget, area, purpose).", "qualification_slots")
        check(bool(bc.offerings or bc.faqs), "knowledge", "Add offerings or FAQs so the assistant has something verified to talk about.", "offerings")
        check(bc.booking.enabled, "booking.enabled", "Enable booking so interested customers can request a visit/appointment.", "booking.enabled")
        priced = [o for o in bc.offerings if o.price_minor is not None or o.price_note or "price" in o.details]
        check(not bc.offerings or bool(priced), "offerings.price", "None of your offerings show a price; customers will be told it's on request.", "offerings.price", blocking=False)
    check(bc.integrations.channel_connected, "integrations.channel", "WhatsApp channel is not connected yet.", "integrations.channel_connected")

    if goal:                                   # the compiled playbook must itself be sane
        from hooze.packs import get_pack
        problems = get_pack(goal).playbook.validate_spec()
        check(not problems, "playbook", "; ".join(problems), "playbook")
    return r

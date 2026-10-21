"""Schema-driven qualification (slot filling).

Extracted from: pd `qualification_filter.py` (637 lines of Abuja-specific regex extractors),
pd `intent_engine.extract_lead_profile`, pd `llm.extract_structured_profile`.

WHY THIS IS DIFFERENT: pd's stuck-lead bug (audit C4: leads with 500-600 messages never
progressing) was caused by REGEX extraction that missed "₦150,000,000" and the district
"Jabi". Here the LLM proposes slot values (`record_slots`), and this module only VALIDATES
and NORMALISES them (money, enums, numbers). No location list, no budget regex is
load-bearing for progress. On top of that the audit's invariant is enforced structurally:
after `stuck_after_user_messages` messages with required slots missing, a human is
alerted — a lead can no longer sit in a pre-search stage invisibly.
"""
from __future__ import annotations
import re
from typing import Any, Optional
from pydantic import BaseModel, Field

from hooze.capabilities.base import Capability, register
from hooze.runtime.actions import Tool
from hooze.runtime.models import ToolResult

_MONEY_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(thousand|billion|million|bn|mn|k|m|b)?\b", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}


def normalize_money(text: Any) -> Optional[int]:
    """'₦150,000,000' / '150m' / '150 million' / '1.5bn' / 'N150M' -> integer major units. None if no plausible amount.
    A bare number under 1,000 with no suffix is NOT an amount ('2 bedroom flat for 50m' -> 50,000,000)."""
    if isinstance(text, (int, float)):
        return int(text) if text >= 0 else None
    for m in _MONEY_RE.finditer(str(text or "")):
        raw, suffix = m.group(1).replace(",", ""), (m.group(2) or "").lower()
        try:
            val = float(raw) * (_MULT[suffix] if suffix else 1)
        except ValueError:
            continue
        if suffix or val >= 1000:
            return int(round(val))
    return None


def _num(text: Any) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))
    return float(m.group()) if m else None


def coerce_slot(spec: dict, raw: Any) -> tuple[bool, Any]:
    t = spec.get("type", "text")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return False, None
    if t == "money":
        v = normalize_money(raw)
        return (v is not None), v
    if t == "number":
        v = _num(raw)
        return (v is not None), v
    if t == "bool":
        s = str(raw).strip().lower()
        if s in {"yes", "true", "y", "1"}: return True, True
        if s in {"no", "false", "n", "0"}: return True, False
        return False, None
    if t == "enum":
        s = str(raw).strip().lower()
        for v in spec.get("values", []):
            if v.lower() == s:
                return True, v
        alias = {a.lower(): v for v, al in (spec.get("aliases") or {}).items() for a in al}
        if s in alias:
            return True, alias[s]
        return False, None
    return True, str(raw).strip()[:300]


class _RecordSlots(BaseModel):
    slots: dict[str, Any] = Field(default_factory=dict)


@register
class QualificationCapability(Capability):
    name = "qualification"

    def specs(self) -> list[dict]:
        return self.config.get("slots", [])

    def _known(self, contact: dict) -> dict:
        return ((contact.get("attributes") or {}).get("slots")) or {}

    def missing(self, contact: dict) -> list[dict]:
        known = self._known(contact)
        return [s for s in self.specs() if s.get("required", True) and known.get(s["name"]) in (None, "")]

    def is_complete(self, contact: dict) -> bool:
        return bool(self.specs()) and not self.missing(contact)

    def tools(self) -> list[Tool]:
        async def record_slots(turn, args: _RecordSlots) -> ToolResult:
            by_name = {s["name"]: s for s in self.specs()}
            stored, rejected = {}, []
            for k, raw in args.slots.items():
                spec = by_name.get(k)
                if not spec:
                    rejected.append(k); continue
                ok, val = coerce_slot(spec, raw)
                (stored.__setitem__(k, val) if ok else rejected.append(k))
            was_complete = self.is_complete(turn.contact)
            if stored:
                await turn.store.merge_slots(turn.contact, stored)
            complete = self.is_complete(turn.contact)
            if complete and not was_complete:
                await turn.events.info("QUALIFICATION_COMPLETE", tenant_id=turn.tenant_id, trace_id=turn.trace_id, ref_type="contact", ref_id=turn.contact["id"])
            return ToolResult(data={"stored": stored, "rejected": rejected, "complete": complete,
                                    "missing": [s["name"] for s in self.missing(turn.contact)]})
        names = ", ".join(s["name"] for s in self.specs())
        return [Tool("record_slots", f"save facts the customer stated; keys must be from: {names}", _RecordSlots, record_slots, self.name)]

    async def context(self, turn) -> str:
        known, missing = self._known(turn.contact), self.missing(turn.contact)
        lines = ["KNOWN ABOUT THE CUSTOMER (never re-ask):"]
        lines += [f"- {k}: {v}" for k, v in known.items()] or ["- (nothing yet)"]
        if missing:
            lines.append("STILL NEEDED (ask naturally, one at a time, only if it helps; infer instead of interrogating when the customer already implied it):")
            lines += [f"- {s['name']}: {s.get('label', s['name'])}" + (f" ({s['ask']})" if s.get("ask") else "") for s in missing]
        else:
            lines.append("All required information is collected — move the conversation forward; do not ask more qualifying questions.")
        return "\n".join(lines)

    async def after_turn(self, turn) -> None:
        """Escalate a lead that has STALLED: `cap` customer messages with no new answer learned. It used to compare the conversation's
        LIFETIME message count to the cap, so once a chat passed it, EVERY turn escalated (and paused the assistant) even while the
        customer was answering (real logs: slots 3 -> 2 -> 1 missing, escalated each time). Progress is tracked on the contact."""
        cap = self.config.get("stuck_after_user_messages")
        if not (cap and self.specs()) or self.is_complete(turn.contact):
            return
        count = turn.conversation.get("user_message_count", 0)
        known_n = sum(1 for v in self._known(turn.contact).values() if v not in (None, ""))
        attrs = dict(turn.contact.get("attributes") or {})
        prog = attrs.get("qual_progress") or {}
        if not prog:                                   # first look (new chat, or a contact from before this tracking): a fresh allowance
            prog = {"known": known_n, "at": max(count - 1, 0)}
        elif known_n > prog.get("known", 0):           # the customer just told us something new
            prog = {"known": known_n, "at": count}
        if prog != attrs.get("qual_progress"):
            attrs["qual_progress"] = prog
            turn.contact["attributes"] = attrs
            await turn.store.set_contact(turn.contact["id"], attributes=attrs)
        if count - prog["at"] >= cap:
            await turn.capabilities["handoff"].escalate(turn, "QUALIFICATION_STUCK", missing=[s["name"] for s in self.missing(turn.contact)])

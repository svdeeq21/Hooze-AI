"""Config normalizer: turns a *near-miss* config (hand-written, AI-generated, older version) into a valid BusinessConfig.

Why this exists: onboarding configs are often produced by an LLM or typed by hand. They are usually *almost* right
(slots as bare strings, `price` instead of `price_minor`, lists inside `identity.facts`, missing `media` arrays...).
Rejecting those with a 422 helps nobody, so we repair what is unambiguous and report every repair in `fixes`.

What we deliberately do NOT do: invent business facts. Missing prices, phone numbers, or `sales_process.goal` stay
missing so the readiness validator can ask for them. The TypeScript twin is ui/src/lib/configNormalize.ts — keep the
two in sync (tests on both sides pin the same cases).
"""
from __future__ import annotations
import re
from typing import Any

from pydantic import ValidationError

from hooze.config_engine.schema import BusinessConfig

SLOT_TYPES = ("text", "number", "money", "enum", "bool")
GOALS = ("order", "consult_and_book")
KNOWN_TOP = ("identity", "offerings", "faqs", "qualification_slots", "stuck_after_user_messages", "sales_process",
             "booking", "hours", "handoff", "communication", "restrictions", "integrations")


class ConfigProblem(Exception):
    """The config is not repairable. `problems` are human-readable, one per line, with the path that is wrong."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _s(v: Any) -> str:
    if v is None or isinstance(v, (dict, list)):
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v).strip()


def _str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.splitlines() if x.strip()]
    if isinstance(v, (list, tuple)):
        return [t for t in (_s(x) for x in v) if t]
    return []


def _obj(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _title(s: str) -> str:
    return _snake(s).replace("_", " ").capitalize()


def _int(v: Any, default: int) -> int:
    try:
        n = int(float(v))
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _num(v: Any):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(str(v).replace(",", "").replace("₦", "").strip())
    except ValueError:
        return None


def normalize_raw(raw: Any) -> tuple[dict, list[str]]:
    """Return (clean dict ready for BusinessConfig, fixes). Never raises on shape problems; raises ConfigProblem only if
    the input is not an object at all."""
    if not isinstance(raw, dict):
        raise ConfigProblem(["The configuration must be a JSON object ({ ... })."])
    fixes: list[str] = []
    fix = fixes.append

    for k in raw:
        if k not in KNOWN_TOP:
            fix(f"Ignored unknown field \"{k}\".")

    # ── identity ─────────────────────────────────────────────
    i = _obj(raw.get("identity"))
    langs = _str_list(i.get("languages")) or ["English"]
    facts_raw = i.get("facts")
    facts: dict[str, str] = {}
    if isinstance(facts_raw, dict):
        for k, v in facts_raw.items():
            if isinstance(v, (list, tuple)):
                t = ", ".join(x for x in (_s(y) for y in v) if x)
            else:
                t = _s(v)
            if t:
                facts[str(k)] = t
            else:
                fix(f"identity.facts.{k} was empty or not text, so it was dropped.")
            if isinstance(v, (list, tuple)) and t:
                fix(f"identity.facts.{k} was a list; joined into one text value.")
    elif facts_raw not in (None, ""):
        fix("identity.facts must be an object of text values; it was reset to empty.")
    identity = {"name": _s(i.get("name")), "industry": _s(i.get("industry")), "description": _s(i.get("description")),
                "website": _s(i.get("website")), "address": _s(i.get("address")), "languages": langs,
                "timezone": _s(i.get("timezone")) or "Africa/Lagos", "currency": _s(i.get("currency")) or "NGN", "facts": facts}

    # ── offerings ────────────────────────────────────────────
    offerings: list[dict] = []
    for n, o in enumerate(raw.get("offerings") or []):
        if isinstance(o, str):
            o = {"name": o}
            fix(f"offerings[{n}] was plain text; turned into an offering named \"{o['name']}\".")
        if not isinstance(o, dict) or not _s(o.get("name")):
            fix(f"offerings[{n}] had no name, so it was dropped.")
            continue
        price_minor = o.get("price_minor")
        if price_minor is None and o.get("price") is not None:
            p = _num(o.get("price"))
            if p is not None:
                price_minor = round(p * 100)
                fix(f"offerings[{n}] used \"price\" ({p:g}); read it as naira and stored {price_minor} in kobo (price_minor).")
        pm = _num(price_minor)
        groups: list[dict] = []
        for gi, g in enumerate(o.get("option_groups") or []):
            if not isinstance(g, dict) or not _s(g.get("name")):
                fix(f"offerings[{n}].option_groups[{gi}] had no name, so it was dropped.")
                continue
            opts = []
            for op in g.get("options") or []:
                if isinstance(op, str):
                    opts.append({"name": op.strip(), "price_minor": 0})
                elif isinstance(op, dict) and _s(op.get("name")):
                    opts.append({"name": _s(op.get("name")), "price_minor": int(_num(op.get("price_minor")) or 0)})
            mi = _num(g.get("max_included"))
            groups.append({"name": _s(g.get("name")), "selection": "multiple" if g.get("selection") == "multiple" else "single",
                           "required": bool(g.get("required")), "max_included": int(mi) if mi is not None else None, "options": opts})
        details = {str(k): _s(v) for k, v in _obj(o.get("details")).items() if _s(v)}
        offerings.append({"name": _s(o.get("name")), "description": _s(o.get("description")),
                          "price_minor": int(pm) if pm is not None else None, "price_note": _s(o.get("price_note")),
                          "category": _s(o.get("category")), "url": _s(o.get("url")), "media": _str_list(o.get("media")),
                          "details": details, "option_groups": groups})

    # ── faqs ─────────────────────────────────────────────────
    faqs: list[dict] = []
    for n, f in enumerate(raw.get("faqs") or []):
        f = _obj(f)
        q, a = _s(f.get("question") or f.get("q")), _s(f.get("answer") or f.get("a"))
        if q and a:
            faqs.append({"question": q, "answer": a})
        else:
            fix(f"faqs[{n}] needs both a question and an answer, so it was dropped.")

    # ── qualification slots ──────────────────────────────────
    slots: list[dict] = []
    seen: set[str] = set()
    for n, s in enumerate(raw.get("qualification_slots") or []):
        if isinstance(s, str):
            name = _snake(s)
            s = {"name": name, "label": _title(s), "required": False}
            fix(f"qualification_slots[{n}] was plain text (\"{name}\"); turned into an optional text question. Edit it to add the question wording.")
        s = _obj(s)
        name = _snake(_s(s.get("name")))
        if not name or name in seen:
            fix(f"qualification_slots[{n}] had no usable unique name, so it was dropped.")
            continue
        seen.add(name)
        typ = s.get("type") if s.get("type") in SLOT_TYPES else "text"
        values = _str_list(s.get("values"))
        if typ == "enum" and not values:
            typ = "text"
            fix(f"qualification_slots[{n}] ({name}) was a choice with no choices; changed to free text.")
        aliases = {str(k): _str_list(v) for k, v in _obj(s.get("aliases")).items()}
        slots.append({"name": name, "label": _s(s.get("label")) or _title(name), "type": typ, "values": values,
                      "aliases": aliases, "required": s.get("required") is not False, "ask": _s(s.get("ask"))})

    # ── sales process / booking / handoff / communication / restrictions / integrations ──
    sp = _obj(raw.get("sales_process"))
    goal = sp.get("goal")
    if goal not in GOALS:
        if goal not in (None, ""):
            fix(f"sales_process.goal \"{goal}\" is not valid (use \"order\" or \"consult_and_book\"); it was cleared.")
        goal = None
    b = _obj(raw.get("booking"))
    h = _obj(raw.get("handoff"))
    c = _obj(raw.get("communication"))
    r = _obj(raw.get("restrictions"))
    ig = _obj(raw.get("integrations"))

    hours_raw = raw.get("hours")
    hours: dict[str, Any] = {}
    if isinstance(hours_raw, (list, tuple)) and len(hours_raw) == 2:
        hours_raw = {"default": list(hours_raw)}
        fix("hours was a plain [open, close] pair; wrapped as {\"default\": [open, close]}.")
    if isinstance(hours_raw, dict):
        for k, v in hours_raw.items():
            if v is None:
                hours[str(k)] = None
            elif isinstance(v, (list, tuple)) and len(v) == 2 and all(re.fullmatch(r"\d{1,2}:\d{2}", _s(x)) for x in v):
                hours[str(k)] = [_s(x) for x in v]
            else:
                fix(f"hours.{k} must look like [\"09:00\", \"18:00\"] or null; it was dropped.")
    elif hours_raw not in (None, ""):
        fix("hours must be an object like {\"default\": [\"09:00\", \"18:00\"]}; it was reset.")

    payments = ig.get("payments")
    clean = {
        "identity": identity, "offerings": offerings, "faqs": faqs, "qualification_slots": slots,
        "stuck_after_user_messages": _int(raw.get("stuck_after_user_messages"), 14),
        "sales_process": {"goal": goal, "notes": _s(sp.get("notes"))},
        "booking": {"enabled": bool(b.get("enabled")), "subject_label": _s(b.get("subject_label")) or "appointment"},
        "hours": hours,
        "handoff": {"contact_name": _s(h.get("contact_name")), "staff_phones": _str_list(h.get("staff_phones")),
                    "human_request_patterns": _str_list(h.get("human_request_patterns"))},
        "communication": {"tone": _s(c.get("tone")) or "warm, brief and professional", "greeting": _s(c.get("greeting")),
                          "rules": _str_list(c.get("rules"))},
        "restrictions": {"never_say": _str_list(r.get("never_say")), "prohibited_claims": _str_list(r.get("prohibited_claims"))},
        "integrations": {"payments": _s(payments) or None, "payments_connected": bool(ig.get("payments_connected")),
                         "channel_connected": bool(ig.get("channel_connected"))},
    }
    return clean, fixes


def parse_config(raw: Any) -> tuple[BusinessConfig, list[str]]:
    """Normalize, then validate. Raises ConfigProblem with readable, path-prefixed messages if something is still wrong."""
    clean, fixes = normalize_raw(raw)
    try:
        return BusinessConfig.model_validate(clean), fixes
    except ValidationError as e:   # pragma: no cover - normalize makes this very unlikely; kept as a readable backstop
        raise ConfigProblem([f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]) from e

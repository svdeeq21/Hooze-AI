"""Customer-facing order messages: the menu, one item, the checkout summary and the payment receipt.

These are built by CODE, not improvised by the model, so they are always laid out the same tidy way, always show the
real prices, and can never invent an item. WhatsApp formatting only: *bold*, _italic_, line breaks, emoji.
"""
from __future__ import annotations
from typing import Optional

from hooze.payments.base import fmt_money

MAX_CHUNK = 3200                      # WhatsApp allows ~4096 characters; leave headroom and split on section boundaries
RULE = "━━━━━━━━━━━━"


def _mods(it: dict) -> str:
    names = [m.get("name", "") for m in (it.get("modifiers") or []) if m.get("name")]
    return f" ({', '.join(names)})" if names else ""


def _lines(items: list[dict], cur: str) -> str:
    return "\n".join(f"• {i['quantity']}× {i['product_name']}{_mods(i)} — {fmt_money(i['line_total_minor'], cur)}" for i in items)


def _product_block(p: dict, cur: str) -> str:
    out = f"▫️ *{p['name']}* — {fmt_money(p['price_minor'], cur)}"
    desc = (p.get("description") or "").strip()
    if desc:
        out += f"\n    _{desc}_"
    required = [g["name"] for g in (p.get("groups") or []) if g.get("required")]
    if required:
        out += f"\n    ✏️ _You'll choose: {', '.join(required)}_"
    return out


def format_menu(business: str, catalog: list[dict], cur: str) -> list[str]:
    """The menu as one or more WhatsApp messages. Sections follow each product's category (in menu order); with no
    categories it is a single tidy list."""
    if not catalog:
        return ["We don't have anything listed right now. Would you like me to get someone from the team to help?"]
    order: list[str] = []
    by_cat: dict[str, list[dict]] = {}
    for p in catalog:
        c = (p.get("category") or "").strip()
        if c not in by_cat:
            by_cat[c] = []
            order.append(c)
        by_cat[c].append(p)
    sections = []
    for c in order:
        blocks = [_product_block(p, cur) for p in by_cat[c]]
        body = ("\n\n" if any("\n" in b for b in blocks) else "\n").join(blocks)      # compact when items are one-liners
        sections.append(f"*{c.upper()}*\n{body}" if c else body)
    header = f"📋 *{business} — Menu*" if business else "📋 *Menu*"
    footer = "👉 Tell me what you'd like and I'll add it to your order."
    chunks: list[str] = []
    cur_chunk = header
    for s in sections:
        if len(cur_chunk) + len(s) + 2 > MAX_CHUNK and cur_chunk != header:
            chunks.append(cur_chunk)
            cur_chunk = ""
        cur_chunk = f"{cur_chunk}\n\n{s}".strip("\n")
    chunks.append(f"{cur_chunk}\n\n{footer}")
    return chunks


def format_item_caption(p: dict, cur: str) -> str:
    cap = f"*{p['name']}* — {fmt_money(p['price_minor'], cur)}"
    desc = (p.get("description") or "").strip()
    return f"{cap}\n_{desc}_" if desc else cap


def _where(method: Optional[str], address: Optional[str]) -> str:
    return f"🚴 Delivery to: {address}" if method == "DELIVERY" and address else "🚴 Delivery" if method == "DELIVERY" else "🏠 Pickup"


def format_checkout(items: list[dict], subtotal_minor: int, method: Optional[str], address: Optional[str], pay_url: str, cur: str) -> str:
    msg = (f"🧾 *Your order*\n{_lines(items, cur)}\n{RULE}\n*Total: {fmt_money(subtotal_minor, cur)}*\n{_where(method, address)}\n\n"
           f"💳 Tap to pay securely:\n{pay_url}")
    if method == "DELIVERY":
        msg += "\n\n_(This covers the food only — we'll confirm your delivery fee separately once payment goes through.)_"
    return msg


def format_receipt(business: str, items: list[dict], subtotal_minor: int, method: Optional[str], cur: str) -> str:
    nxt = ("🚴 We'll reach out shortly to confirm your delivery fee and arrange delivery." if method == "DELIVERY"
           else "🏠 We'll message you the moment it's ready for pickup.")
    thanks = f"Thank you for choosing *{business}*! 🙏" if business else "Thank you! 🙏"
    return f"✅ *Payment received — thank you!*\n{_lines(items, cur)}\n{RULE}\n*Paid: {fmt_money(subtotal_minor, cur)}*\n\n{nxt}\n\n{thanks}"

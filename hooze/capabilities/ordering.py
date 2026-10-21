"""Ordering: catalog + modifiers + cart + checkout + payment confirmation.

Extracted from: savoury `orders.py` (802 lines), `pricing_engine.py`, `catalog.py`,
`message_pipeline._apply_action / _handle_pending_payment_message / _start_checkout /
handle_confirmed_payment`, `paystack.py`, `admin_commands` (sold-out).

Every fix from savoury's three real-transcript reviews is preserved as structure:
  * PERSISTED DRAFT ITEM — each partial answer merges into `orders.draft`; nothing is discarded, the
    customer is told ALL remaining choices + real options at once (deterministic, from the catalog).
  * PAYMENT-PENDING GATE — while a checkout is in flight the LLM is bypassed entirely (it used to
    improvise "Enjoy your order!" with no payment made). Cancel / "same order again" is code-driven.
  * EVERY cart mutation bumps `updated_at` (a missed bump silently expired carts mid-conversation).
  * HONEST OVERRIDES — an unresolvable product returns an authoritative "couldn't match" message.
  * EXACTLY-ONCE PAYMENT — a conditional UPDATE (`WHERE status='PAYMENT_PENDING'`) is the idempotency
    guard, so a redelivered webhook is a no-op even across workers. Amount is compared, not just status.
  * FOOD-ONLY CHARGE — checkout charges the subtotal; delivery fee is confirmed by a human afterwards.
Changes: line refs (#1, #2) instead of UUIDs in prompts; integer minor units; per-tenant payments.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, Field

from hooze.capabilities.base import Capability, GateResult, StaffCommand, register
from hooze.capabilities.order_messages import format_checkout, format_item_caption, format_menu, format_receipt
from hooze.kernel.db import first
from hooze.payments.base import fmt_money
from hooze.runtime import guards
from hooze.runtime.actions import Tool
from hooze.runtime.models import BusinessRuleError, ToolResult

CANCEL_RE = re.compile(r"\b(cancel|start over|never\s*mind|nevermind|forget it|undo)\b", re.I)
REPEAT_RE = re.compile(r"\b(again|same (order|thing)|repeat (that|it|the order)|do (it|that) again)\b", re.I)
ADD_CLAIM = [r"\b(added|adding|i'?ve added|removed|removing|updated your (cart|order))\b"]


def line_total_minor(unit_minor: int, modifier_minors: list[int], quantity: int) -> int:
    """Pure. (base + sum(modifiers)) x quantity. The LLM never does this arithmetic."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    return (int(unit_minor) + sum(int(m) for m in modifier_minors)) * int(quantity)


class _Add(BaseModel):
    product_name: str
    modifier_names: list[str] = Field(default_factory=list)
    quantity: Optional[int] = None      # None => keep the draft's quantity (a follow-up answer must not reset '2 boxes' to 1)

class _Line(BaseModel):
    line: int

class _Qty(BaseModel):
    line: int
    quantity: int

class _Fulfil(BaseModel):
    method: str
    delivery_address: Optional[str] = None
    delivery_area: Optional[str] = None

class _NoArgs(BaseModel):
    pass


class _Show(BaseModel):
    product_name: str = Field("", description="exact menu item name")


def _find(rows: list[dict], name: str) -> Optional[dict]:
    n = (name or "").strip().lower()
    return next((r for r in rows if r["name"].strip().lower() == n), None) or next((r for r in rows if n and n in r["name"].strip().lower()), None)


@register
class OrderingCapability(Capability):
    name = "ordering"

    # ── data access ───────────────────────────────────────────────
    async def load_catalog(self, include_unavailable: bool = False) -> list[dict]:
        """Capability instances live for ONE turn, so this cache is per-turn: the menu was being loaded twice per ordering turn
        (prompt context + add_product). Staff commands that mutate availability drop it."""
        cache = self.__dict__.setdefault("_catalog_cache", {})
        if include_unavailable not in cache:
            cache[include_unavailable] = await self._load_catalog(include_unavailable)
        return cache[include_unavailable]

    async def _load_catalog(self, include_unavailable: bool) -> list[dict]:
        db = self.ctx.db
        def q(t):
            b = db.table(t).select("*")
            return b if include_unavailable else b.eq("available", True)
        products = (await q("products").execute()).data or []
        groups = (await db.table("modifier_groups").select("*").execute()).data or []
        mods = (await q("modifiers").execute()).data or []
        for p in sorted(products, key=lambda x: x.get("sort_order", 0)):
            p["groups"] = sorted([{**g, "modifiers": sorted([m for m in mods if m["group_id"] == g["id"]], key=lambda m: m.get("sort_order", 0))}
                                  for g in groups if g["product_id"] == p["id"]], key=lambda g: g.get("sort_order", 0))
        return sorted(products, key=lambda x: x.get("sort_order", 0))

    async def _touch(self, order_id: str, **fields) -> None:
        await self.ctx.db.table("orders").update({**fields, "updated_at": self.ctx.clock().isoformat()}).eq("id", order_id).execute()

    def _is_stale(self, row: dict) -> bool:
        return self.ctx.clock() - datetime.fromisoformat(row["updated_at"]) > timedelta(hours=self.config.get("cart_stale_after_hours", 4))

    async def find_cart(self, contact_id: str) -> Optional[dict]:
        """READ-ONLY. Reading the menu must never create an order row (it used to: one empty CART per visitor)."""
        row = first(await self.ctx.db.table("orders").select("*").eq("contact_id", contact_id).eq("status", "CART").limit(1).execute())
        return None if (row and self._is_stale(row)) else row

    async def get_cart(self, contact_id: str) -> dict:
        """Get-or-create, for MUTATING tools only. Expires a stale cart before opening a fresh one."""
        row = first(await self.ctx.db.table("orders").select("*").eq("contact_id", contact_id).eq("status", "CART").limit(1).execute())
        if row and self._is_stale(row):
            await self.ctx.db.table("orders").update({"status": "EXPIRED"}).eq("id", row["id"]).execute()
            row = None
        if not row:
            row = (await self.ctx.db.table("orders").insert({"contact_id": contact_id, "status": "CART", "subtotal_minor": 0, "delivery_fee_minor": 0,
                                                              "draft": None, "updated_at": self.ctx.clock().isoformat()}).execute()).data[0]
        return row

    async def items(self, order_id: str) -> list[dict]:
        rows = (await self.ctx.db.table("order_items").select("*").eq("order_id", order_id).order("created_at").execute()).data or []
        return rows

    async def _recalc(self, order_id: str) -> None:
        subtotal = sum(i["line_total_minor"] for i in await self.items(order_id))
        await self._touch(order_id, subtotal_minor=subtotal)

    # ── draft item logic (savoury's fix, kept) ────────────────────
    async def _update_draft(self, cart: dict, product: dict, qty: Optional[int], new_mods: list[dict]) -> dict:
        draft = cart.get("draft") or {}
        if draft.get("product_id") != product["id"]:
            draft = {"product_id": product["id"], "modifier_ids": [], "quantity": max(1, qty or 1)}
        elif qty is not None:
            draft["quantity"] = max(1, qty)
        qty = draft["quantity"]
        group_of = {m["id"]: g for g in product["groups"] for m in g["modifiers"]}
        selected = set(draft["modifier_ids"])
        for m in new_mods:
            g = group_of[m["id"]]
            if g["selection"] == "single":
                selected -= {x["id"] for x in g["modifiers"]}
            selected.add(m["id"])
        for g in product["groups"]:
            n = len(selected & {x["id"] for x in g["modifiers"]})
            if g.get("max_selections") and n > g["max_selections"]:
                extra = any("extra" in x["name"].lower() for x in product["groups"])
                raise BusinessRuleError(f"{g['name']} includes up to {g['max_selections']} choice(s) and you've picked {n}. Which would you like to drop?"
                                        + (" (Additional ones can be added under Extras for a small charge.)" if extra else ""))
        picked = [m for g in product["groups"] for m in g["modifiers"] if m["id"] in selected]
        missing = [g for g in product["groups"] if g.get("required") and not (selected & {x["id"] for x in g["modifiers"]})]
        if missing:
            draft["modifier_ids"] = sorted(selected)
            await self._touch(cart["id"], draft={**draft, "product_name": product["name"]})
            cart["draft"] = draft
            return {"committed": False, "selected": picked, "missing": missing}
        await self.ctx.db.table("order_items").insert({
            "order_id": cart["id"], "product_id": product["id"], "product_name": product["name"], "unit_price_minor": product["price_minor"], "quantity": qty,
            "modifiers": [{"id": m["id"], "name": m["name"], "price_minor": m["price_minor"]} for m in picked],
            "line_total_minor": line_total_minor(product["price_minor"], [m["price_minor"] for m in picked], qty)}).execute()
        await self._touch(cart["id"], draft=None)
        cart["draft"] = None
        await self._recalc(cart["id"])
        return {"committed": True, "selected": picked, "missing": []}

    @staticmethod
    def _incomplete_message(product: dict, r: dict) -> str:
        parts = []
        for g in r["missing"]:
            opts = ", ".join(m["name"] for m in g["modifiers"])
            parts.append(f"{g['name']} — choose {'1' if g['selection'] == 'single' else 'at least 1'}: {opts}")
        if not r["selected"]:
            return f"Let's build your {product['name']}! Here's what you'll need to choose:\n" + "\n".join(f"- {p}" for p in parts)
        return f"Got it — {', '.join(m['name'] for m in r['selected'])} so far for your {product['name']}. Still need:\n" + "\n".join(f"- {p}" for p in parts)

    # ── tools ─────────────────────────────────────────────────────
    def tools(self) -> list[Tool]:
        cur = lambda: self.ctx.config.currency

        async def add_product(turn, a: _Add) -> ToolResult:
            catalog = await self.load_catalog()
            product = _find(catalog, a.product_name)
            if not product:
                await turn.events.warn("LLM_REFERENCED_UNKNOWN_PRODUCT", tenant_id=turn.tenant_id, name=a.product_name)
                return ToolResult(ok=False, message=f"Sorry, I couldn't match \"{a.product_name or 'that'}\" to anything on our menu — could you tell me again what you'd like?")
            def resolve(n: str):
                allm = [x for g in product["groups"] for x in g["modifiers"]]
                return _find(allm, n)
            mods, unmatched = [], []
            for n in a.modifier_names:
                (mods.append(m) if (m := resolve(n)) else unmatched.append(n))
            cart = await self.get_cart(turn.contact["id"])
            r = await self._update_draft(cart, product, a.quantity, mods)
            note = ("I couldn't find " + ", ".join(f'"{n}"' for n in unmatched) + f" as an option for the {product['name']}. ") if unmatched else ""
            if r["committed"]:
                if unmatched:
                    return ToolResult(data={"committed": True}, message=f"I've added your {product['name']}, but {note[0].lower() + note[1:]}so it's not included. Anything you'd like to change?")
                return ToolResult(data={"committed": True})
            return ToolResult(data={"committed": False}, message=note + self._incomplete_message(product, r))

        async def _line(turn, n: int) -> tuple[dict, dict]:
            cart = await self.find_cart(turn.contact["id"])
            its = await self.items(cart["id"]) if cart else []
            if not 1 <= n <= len(its):
                raise BusinessRuleError(f"I don't see item #{n} in your order. Want me to list what's in it?")
            return cart, its[n - 1]

        async def remove_item(turn, a: _Line) -> ToolResult:
            cart, it = await _line(turn, a.line)
            await self.ctx.db.table("order_items").delete().eq("id", it["id"]).execute()
            await self._recalc(cart["id"])
            return ToolResult()

        async def set_quantity(turn, a: _Qty) -> ToolResult:
            if a.quantity < 1:
                raise BusinessRuleError("Quantity must be at least 1 — or say \"remove\" to take it out.")
            cart, it = await _line(turn, a.line)
            await self.ctx.db.table("order_items").update({"quantity": a.quantity, "line_total_minor": line_total_minor(it["unit_price_minor"], [m["price_minor"] for m in it["modifiers"]], a.quantity)}).eq("id", it["id"]).execute()
            await self._recalc(cart["id"])
            return ToolResult()

        async def set_fulfillment(turn, a: _Fulfil) -> ToolResult:
            method = a.method.upper()
            if method not in ("PICKUP", "DELIVERY"):
                raise BusinessRuleError("Is this for pickup or delivery?")
            cart = await self.get_cart(turn.contact["id"])
            addr = a.delivery_address or cart.get("delivery_address")
            if method == "DELIVERY" and not addr:
                raise BusinessRuleError("What address should we deliver to?")
            await self._touch(cart["id"], fulfillment_method=method, delivery_address=addr if method == "DELIVERY" else None, delivery_area=a.delivery_area)
            return ToolResult()

        async def checkout(turn, a: _NoArgs) -> ToolResult:
            cart = await self.find_cart(turn.contact["id"])
            its = await self.items(cart["id"]) if cart else []
            if not its:
                raise BusinessRuleError("Your cart is empty — what would you like to order?")
            if not cart.get("fulfillment_method"):
                raise BusinessRuleError("Before checkout — will this be pickup or delivery?")
            amount = cart["subtotal_minor"]                      # food only, always (delivery fee confirmed by a human after payment)
            reference = f"{self.ctx.tenant_id[:8]}_{cart['id']}"
            await self._touch(cart["id"], status="PAYMENT_PENDING")
            try:
                provider = await self.ctx.extras["payments"]()
                init = await provider.initialize(reference=reference, amount_minor=amount, currency=cur(), phone=turn.contact["phone"], metadata={"order_id": cart["id"]})
                await self.ctx.db.table("payments").insert({"order_id": cart["id"], "provider": provider.name, "reference": reference, "status": "pending",
                                                            "amount_minor": amount, "currency": cur(), "authorization_url": init["authorization_url"]}).execute()
            except Exception as e:
                await turn.events.error("CHECKOUT_INIT_FAILED", tenant_id=turn.tenant_id, trace_id=turn.trace_id, ref_type="order", ref_id=cart["id"], error=str(e)[:200])
                await self._touch(cart["id"], status="CART")
                raise BusinessRuleError("Sorry, I couldn't start checkout right now — please try again in a moment.")
            msg = format_checkout(its, amount, cart.get("fulfillment_method"), cart.get("delivery_address"), init["authorization_url"], cur())
            return ToolResult(message=msg, data={"order_id": cart["id"]})

        async def show_menu(turn, a: _NoArgs) -> ToolResult:
            chunks = format_menu(self.ctx.config.persona.business_name, await self.load_catalog(), cur())
            for c in chunks[:-1]:                                  # a long menu goes out as several tidy messages
                await turn.out.text(c, typing=False)
            return ToolResult(message=chunks[-1])

        async def show_item(turn, a: _Show) -> ToolResult:
            p = _find(await self.load_catalog(), a.product_name)
            if not p:
                raise BusinessRuleError(f"I couldn't find \"{a.product_name or 'that'}\" on our menu — want me to show you the full menu?")
            media = [m for m in (p.get("media") or []) if m.get("url")][: self.config.get("max_media", 4)]
            for k, m in enumerate(media):
                await turn.out.media(m["url"], m.get("type", "image"), format_item_caption(p, cur()) if k == 0 else "")
            if media:
                return ToolResult(message=f"Would you like me to add *{p['name']}* to your order? 😊")
            return ToolResult(message=format_item_caption(p, cur()) + "\n\nWould you like me to add it to your order?")

        return [
            Tool("show_menu", "send the customer the full, nicely laid-out menu with prices — use whenever they ask what you offer, for the menu, or for prices; never type the menu yourself", _NoArgs, show_menu, self.name),
            Tool("show_item", "send photos/videos and details of ONE menu item (product_name) — use when the customer asks to see a picture or video, or wants more detail on a single item", _Show, show_item, self.name),
            Tool("add_product", "add a product (with any chosen options) to the order; call it as soon as a product is named, even with no options yet", _Add, add_product, self.name),
            Tool("remove_item", "remove cart line number N", _Line, remove_item, self.name),
            Tool("set_quantity", "change quantity of cart line number N", _Qty, set_quantity, self.name),
            Tool("set_fulfillment", "set PICKUP or DELIVERY (delivery needs an address)", _Fulfil, set_fulfillment, self.name),
            Tool("checkout", "start payment — only when fulfilment is set AND the customer clearly said they're ready to pay", _NoArgs, checkout, self.name),
        ]

    # ── prompt context, guards, next step ─────────────────────────
    async def context(self, turn) -> str:
        cur = self.ctx.config.currency
        lines = ["MENU (only these exist; prices are fixed — you cannot change them):"]
        for p in await self.load_catalog():
            lines.append(f"- {p['name']} ({fmt_money(p['price_minor'], cur)}): {p.get('description') or ''}".rstrip(": "))
            for g in p["groups"]:
                mx = g.get("max_selections")
                req = ("required, pick exactly 1" if g["selection"] == "single" and g.get("required") else "optional, pick 1" if g["selection"] == "single"
                       else f"{'required' if g.get('required') else 'optional'}, up to {mx} included free" if mx else "optional, any number")
                opts = ", ".join(f"{m['name']} (+{fmt_money(m['price_minor'], cur)})" if m["price_minor"] else m["name"] for m in g["modifiers"])
                lines.append(f"    {g['name']} [{req}]: {opts}")
        cart = await self.find_cart(turn.contact["id"])
        its = await self.items(cart["id"]) if cart else []
        lines.append("\nCURRENT CART:")
        lines += [f"#{i} {it['quantity']}x {it['product_name']}" + (f" ({', '.join(m['name'] for m in it['modifiers'])})" if it["modifiers"] else "") for i, it in enumerate(its, 1)] or ["(empty)"]
        cart = cart or {}
        lines.append(f"Fulfilment: {cart.get('fulfillment_method') or 'not set yet'}" + (f" to {cart['delivery_address']}" if cart.get("delivery_address") else ""))
        if cart.get("draft") and cart["draft"].get("modifier_ids"):
            lines.append(f"IN PROGRESS (not added yet — still missing something required): {cart['draft'].get('product_name')}")
        lines.append("\nORDERING RULES: (1) Only reference menu items by exact name. (2) A group's 'included free' limit is real: offer the extra via an Extras group instead of refusing. "
                     "(3) Before checkout you must know PICKUP or DELIVERY; delivery needs an address; payment covers food only — for delivery, a person confirms the fee AFTER payment. "
                     "(4) Propose checkout only after fulfilment is set and the customer clearly confirmed they're done. (5) Never state totals; the system shows them.")
        return "\n".join(lines)

    def check_reply(self, turn, reply: str, results: list) -> Optional[str]:
        if any(r.tool in {"add_product", "remove_item", "set_quantity", "set_fulfillment"} and r.ok for r in results):
            return None
        if any(re.search(p, reply, re.I) for p in ADD_CLAIM):
            return guards.strip_sentences(reply, ADD_CLAIM) or "What would you like to order?"
        return None

    async def next_step(self, turn) -> Optional[str]:
        """The one canonical "what can you do now" line, computed from POST-action cart state — never the LLM's improvisation."""
        cart = await self.find_cart(turn.contact["id"])
        its = await self.items(cart["id"]) if cart else []
        if not its:
            return "What would you like to order?"
        return "Want to add anything else, or are you ready to checkout?" + ("" if cart.get("fulfillment_method") else " Also, pickup or delivery?")

    # ── deterministic payment-pending gate ────────────────────────
    async def gate(self, turn) -> Optional[GateResult]:
        cart_or_none = first(await self.ctx.db.table("orders").select("*").eq("contact_id", turn.contact["id"]).eq("status", "PAYMENT_PENDING").limit(1).execute())
        cur = self.ctx.config.currency
        if not cart_or_none:
            return None
        pending = cart_or_none
        pay = first(await self.ctx.db.table("payments").select("*").eq("order_id", pending["id"]).limit(1).execute())
        if CANCEL_RE.search(turn.text):
            await self.ctx.db.table("orders").update({"status": "CANCELLED"}).eq("id", pending["id"]).execute()
            if REPEAT_RE.search(turn.text):
                new = await self.get_cart(turn.contact["id"])
                for it in await self.items(pending["id"]):
                    await self.ctx.db.table("order_items").insert({k: v for k, v in it.items() if k not in ("id", "order_id", "created_at", "tenant_id")} | {"order_id": new["id"]}).execute()
                await self._recalc(new["id"])
                await self._touch(new["id"], fulfillment_method=pending.get("fulfillment_method"), delivery_address=pending.get("delivery_address"))
                total = sum(i["line_total_minor"] for i in await self.items(new["id"]))
                return GateResult(reply=f"Cancelled the old checkout and rebuilt the same order. Total: {fmt_money(total, cur)}. Ready to check out, or want to change anything first?")
            return GateResult(reply="No problem — I've cancelled that checkout. Message me anytime you're ready to start a new order.")
        link = f" Here's the link again:\n{pay['authorization_url']}" if pay and pay.get("authorization_url") else ""
        return GateResult(reply=f"You've got a payment in progress for {fmt_money(pending['subtotal_minor'], cur)}.{link}\n\nI'll confirm automatically the moment it goes through. Say \"cancel\" if you'd rather start over.")

    # ── payment confirmation (called by the payments webhook) ─────
    async def confirm_payment(self, reference: str, verified: dict) -> Optional[dict]:
        pay = first(await self.ctx.db.table("payments").select("*").eq("reference", reference).limit(1).execute())
        if not pay:
            await self.ctx.events.error("PAYMENT_UNKNOWN_REFERENCE", tenant_id=self.ctx.tenant_id, reference=reference)
            return None
        if verified.get("status") != "success":
            return None
        if int(verified.get("amount_minor", -1)) != int(pay["amount_minor"]):
            await self.ctx.events.error("PAYMENT_AMOUNT_MISMATCH", tenant_id=self.ctx.tenant_id, reference=reference, expected=pay["amount_minor"], got=verified.get("amount_minor"))
            return None
        # Exactly-once guard = a conditional UPDATE per source status (each is atomic, so no pre-read race). The normal path is
        # PAYMENT_PENDING -> PAID. A verified, amount-matched payment that arrives AFTER the checkout expired (tick) or was
        # cancelled by the customer is STILL the customer's money: it used to fall through to "duplicate" and vanish silently.
        paid_at, late, won = self.ctx.clock().isoformat(), None, None
        for source in ("PAYMENT_PENDING", "EXPIRED", "CANCELLED"):
            won = (await self.ctx.db.table("orders").update({"status": "PAID", "paid_at": paid_at}).eq("id", pay["order_id"]).eq("status", source).execute()).data
            if won:
                late = source if source != "PAYMENT_PENDING" else None
                break
        if not won:
            await self.ctx.events.info("PAYMENT_DUPLICATE_IGNORED", tenant_id=self.ctx.tenant_id, reference=reference)
            return None                                   # exactly-once: a redelivery finds the row already PAID
        if late:
            await self.ctx.events.warn(f"PAYMENT_AFTER_{late}", tenant_id=self.ctx.tenant_id, reference=reference, ref_type="order", ref_id=pay["order_id"])
        await self.ctx.db.table("payments").update({"status": "success", "verified_at": self.ctx.clock().isoformat()}).eq("id", pay["id"]).execute()
        order = won[0]
        contact = first(await self.ctx.db.table("contacts").select("*").eq("id", order["contact_id"]).limit(1).execute())
        cur = self.ctx.config.currency
        its = await self.items(order["id"])
        summary = ", ".join(f"{i['quantity']}x {i['product_name']}" for i in its)
        extra = f"\n🚴 DELIVERY to: {order.get('delivery_address') or '(no address)'}\n⚠️ Confirm the delivery fee with the customer — not yet charged." if order.get("fulfillment_method") == "DELIVERY" else "\n🏠 PICKUP"
        late_note = (f"\n⚠️ This checkout had {'expired' if late == 'EXPIRED' else 'been cancelled'} before the customer paid — the money WAS received. Fulfil the order or refund it in Paystack.") if late else ""
        await self.ctx.outbox.notify_staff(f"🔔 New paid order — {fmt_money(order['subtotal_minor'], cur)} (food only)\n{(contact or {}).get('display_name') or (contact or {}).get('phone')}\n{summary}{extra}{late_note}")
        if contact:
            msg = format_receipt(self.ctx.config.persona.business_name, its, order["subtotal_minor"], order.get("fulfillment_method"), cur)
            # reply from the number this customer ordered on (a tenant may run several)
            convs = (await self.ctx.db.table("conversations").select("id,channel_id,last_inbound_at").eq("contact_id", contact["id"]).execute()).data or []
            conv = max(convs, key=lambda c: c.get("last_inbound_at") or "", default=None)
            await self.ctx.outbox.send(contact["phone"], msg, conv["channel_id"] if conv else None, conversation_id=conv["id"] if conv else None)
        await self.ctx.events.info("ORDER_PAID", tenant_id=self.ctx.tenant_id, ref_type="order", ref_id=order["id"])
        return order

    async def tick(self) -> dict:
        cutoff = (self.ctx.clock() - timedelta(minutes=self.config.get("pending_expiry_minutes", 30))).isoformat()
        stale = (await self.ctx.db.table("orders").select("id").eq("status", "PAYMENT_PENDING").lt("updated_at", cutoff).execute()).data or []
        for o in stale:
            await self.ctx.db.table("orders").update({"status": "EXPIRED"}).eq("id", o["id"]).eq("status", "PAYMENT_PENDING").execute()
        return {"expired": len(stale)}

    def staff_commands(self) -> list[StaffCommand]:
        async def set_item(m, actor, available: bool) -> str:
            name = m.group("item").strip()
            catalog = await self.load_catalog(include_unavailable=True)
            product = _find(catalog, name)
            mod = None if product else _find([x for p in catalog for g in p["groups"] for x in g["modifiers"]], name)
            target, table = (product, "products") if product else (mod, "modifiers")
            if not target:
                return f"I couldn't find \"{name}\" on the menu."
            await self.ctx.db.table(table).update({"available": available}).eq("id", target["id"]).execute()
            self.__dict__.pop("_catalog_cache", None)
            await self.ctx.events.info("ITEM_AVAILABILITY_CHANGED", tenant_id=self.ctx.tenant_id, actor=actor[:6] + "****", item=target["name"], available=available)
            return f"{'✅' if available else '🚫'} {target['name']} marked {'available' if available else 'sold out'}."
        async def sold_out(m, a): return await set_item(m, a, False)
        async def back(m, a): return await set_item(m, a, True)
        return [StaffCommand("<item> sold out", [r"^(?P<item>.+?)\s+(?:is\s+)?sold\s*out$", r"^(?P<item>.+?)\s+(?:is\s+)?(?:finished|done|out)$", r"^(?:no more|out of|we'?re out of)\s+(?P<item>.+)$"], sold_out),
                StaffCommand("<item> back", [r"^(?P<item>.+?)\s+(?:is\s+)?(?:available|back|back in stock)$"], back)]

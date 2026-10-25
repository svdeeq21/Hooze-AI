"""Customer privacy: look up, export and erase one customer's personal data, and an optional retention rule for old chats.

ERASURE ANONYMISES, it does not delete the person's orders: orders / order items / payments are the business's own sales and tax records
(and in the database they hang off the contact, so deleting the contact would delete them too). What is removed is everything that
identifies or quotes the person: the phone number, name, collected details, every message, bookings, follow-up state, delivery
addresses, and any queued raw webhook that contains their number. The paid amounts, items and payment references stay.

Whether, and for how long, you must keep records is a legal question for your lawyer; this code only does what you decide.
"""
from __future__ import annotations
import json, re
from datetime import timedelta
from typing import Optional


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


async def _contact(db, phone: str) -> Optional[dict]:
    rows = (await db.table("contacts").select("*").eq("phone", digits(phone)).limit(1).execute()).data or []
    return rows[0] if rows else None


async def _messages(db, contact_id: str, conv_ids: list[str], columns: str) -> list[dict]:
    """ALL of a customer's messages. The assistant's own replies carry no contact_id (it is optional on messages), so they can only be found
    through the customer's conversations; finding messages by contact_id alone would leave behind every reply that quotes their name."""
    rows: dict = {}
    for r in (await db.table("messages").select(columns).eq("contact_id", contact_id).execute()).data or []:
        rows[r["id"]] = r
    for cv in conv_ids:
        for r in (await db.table("messages").select(columns).eq("conversation_id", cv).execute()).data or []:
            rows[r["id"]] = r
    return sorted(rows.values(), key=lambda r: str(r.get("created_at") or ""))


async def export_customer(db, phone: str) -> Optional[dict]:
    """Everything held about this customer (the right of access). Returns None if there is no such customer."""
    c = await _contact(db, phone)
    if not c:
        return None
    convs = (await db.table("conversations").select("*").eq("contact_id", c["id"]).execute()).data or []
    msgs = await _messages(db, c["id"], [v["id"] for v in convs], "*")
    orders = (await db.table("orders").select("*").eq("contact_id", c["id"]).execute()).data or []
    items = [i for o in orders for i in ((await db.table("order_items").select("*").eq("order_id", o["id"]).execute()).data or [])]
    pays = [p for o in orders for p in ((await db.table("payments").select("*").eq("order_id", o["id"]).execute()).data or [])]
    books = (await db.table("bookings").select("*").eq("contact_id", c["id"]).execute()).data or []
    return {
        "contact": {k: c.get(k) for k in ("phone", "display_name", "attributes", "opted_out", "created_at")},
        "conversations": [{k: v.get(k) for k in ("state", "status", "summary", "created_at", "last_inbound_at")} for v in convs],
        "messages": [{k: m.get(k) for k in ("direction", "sender_type", "content", "content_type", "created_at")} for m in msgs],
        "orders": [{k: o.get(k) for k in ("status", "subtotal_minor", "fulfillment_method", "delivery_address", "paid_at", "created_at")} for o in orders],
        "order_items": [{k: i.get(k) for k in ("product_name", "quantity", "unit_price_minor", "line_total_minor", "modifiers")} for i in items],
        "payments": [{k: p.get(k) for k in ("reference", "status", "amount_minor", "currency", "created_at")} for p in pays],
        "bookings": [{k: b.get(k) for k in ("subject", "preferred_text", "status", "created_at")} for b in books],
    }


async def erase_customer(db, phone: str, *, dry_run: bool = True, scan_limit: int = 5000) -> Optional[dict]:
    c = await _contact(db, phone)
    if not c:
        return None
    cid, num = c["id"], digits(phone)
    convs = (await db.table("conversations").select("id").eq("contact_id", cid).execute()).data or []
    msgs = await _messages(db, cid, [v["id"] for v in convs], "id")
    books = (await db.table("bookings").select("id").eq("contact_id", cid).execute()).data or []
    orders = (await db.table("orders").select("id,delivery_address,draft").eq("contact_id", cid).execute()).data or []
    queued = [e["id"] for e in ((await db.table("inbound_events").select("id,payload").limit(scan_limit).execute()).data or []) if num in json.dumps(e.get("payload") or {})]
    report = {"messages": len(msgs), "conversations": len(convs), "bookings": len(books), "queued_webhooks": len(queued),
              "orders_kept_but_scrubbed": len(orders), "dry_run": dry_run}
    if dry_run:
        return report
    for table, rows in (("messages", msgs), ("bookings", books)):
        for r in rows:
            await db.table(table).delete().eq("id", r["id"]).execute()
    for cv in convs:
        await db.table("nudge_state").delete().eq("conversation_id", cv["id"]).execute()
        await db.table("conversations").delete().eq("id", cv["id"]).execute()
    for qid in queued:
        await db.table("inbound_events").delete().eq("id", qid).execute()
    for o in orders:
        await db.table("orders").update({"delivery_address": None, "delivery_area": None, "draft": None}).eq("id", o["id"]).execute()
    # Last, so an interrupted run can simply be repeated (the phone number is how we find the customer again).
    await db.table("contacts").update({"phone": f"erased-{cid[:8]}", "display_name": None, "attributes": {}, "opted_out": True, "ai_paused": True}).eq("id", cid).execute()
    return report


async def apply_retention(pdb, days: int, now, *, batch: int = 500, max_batches: int = 20) -> dict:
    """Delete messages older than `days` (all clients), and clear the running summary of any chat that lost messages (the summary is made FROM
    them, and its position marker no longer points at the right place). days <= 0 disables it. Contacts, orders and payments are untouched."""
    if days <= 0:
        return {"enabled": False, "messages": 0, "conversations_cleared": 0}
    cutoff = (now - timedelta(days=days)).isoformat()
    deleted, touched = 0, set()
    for _ in range(max_batches):
        rows = (await pdb.table("messages").select("id,conversation_id").lt("created_at", cutoff).limit(batch).execute()).data or []
        if not rows:
            break
        await pdb.table("messages").delete().in_("id", [r["id"] for r in rows]).execute()
        deleted += len(rows)
        touched |= {r["conversation_id"] for r in rows}
        if len(rows) < batch:
            break
    if touched:
        await pdb.table("conversations").update({"summary": "", "summary_upto": 0}).in_("id", sorted(touched)).execute()
    return {"enabled": True, "messages": deleted, "conversations_cleared": len(touched)}

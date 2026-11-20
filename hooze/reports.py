"""The weekly report owners get on WhatsApp: proof, in their own numbers, that the assistant is working for them.

Sent on Monday from 08:00 (the client's own timezone) by the regular /internal/tick sweep, to the client's staff numbers. A row in
`report_log` is RESERVED before sending (unique per client per ISO week), so an overlapping or repeated tick can never send it twice;
if the send itself fails it is logged, not retried (one missed report beats a report loop). Test chats are left out of every number.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from hooze.capabilities.availability import resolve_open, to_local
from hooze.kernel.db import first, is_unique_violation
from hooze.payments.base import fmt_money

WEEKLY = "weekly"


def iso_week(local: datetime) -> str:
    y, w, _ = local.isocalendar()
    return f"{y}-W{w:02d}"


async def _count(q) -> int:
    return int((await q.limit(1).execute()).count or 0)


async def weekly_numbers(pdb, tenant_id: str, cfg, now: datetime) -> dict:
    """The last 7 days, real customers only (the web test channel is excluded everywhere)."""
    since = (now - timedelta(days=7)).isoformat()
    db = pdb.for_tenant(tenant_id)
    web = first(await pdb.table("channels").select("id").eq("tenant_id", tenant_id).eq("kind", "web_demo").limit(1).execute())
    web_id = web["id"] if web else "00000000-0000-0000-0000-000000000000"
    test_contacts: set[str] = set()
    if web:
        test_contacts = {r["contact_id"] for r in (await db.table("conversations").select("contact_id").eq("channel_id", web_id).execute()).data or []}

    chats = await _count(db.table("conversations").select("id", count="exact").neq("channel_id", web_id).gte("last_inbound_at", since))
    inbound_q = lambda: db.table("messages").select("created_at", count="exact").eq("direction", "inbound").neq("channel_id", web_id).gte("created_at", since)
    inbound = await _count(inbound_q())
    after_hours = 0
    if cfg.hours and inbound:
        for start in range(0, 20_000, 1000):           # timestamps only, paged; a week of messages for one business fits easily
            rows = (await inbound_q().order("created_at").range(start, start + 999).execute()).data or []
            for r in rows:
                ts = datetime.fromisoformat(str(r["created_at"]).replace("Z", "+00:00"))
                if not resolve_open(None, cfg.hours, to_local(ts, cfg.timezone))[0]:
                    after_hours += 1
            if len(rows) < 1000:
                break
    paid = [o for o in (await db.table("orders").select("contact_id, subtotal_minor").gte("paid_at", since).execute()).data or []
            if o["contact_id"] not in test_contacts]
    bookings = [b for b in (await db.table("bookings").select("contact_id").gte("created_at", since).execute()).data or []
                if b["contact_id"] not in test_contacts]
    handoffs = await _count(pdb.table("events").select("id", count="exact").eq("tenant_id", tenant_id).eq("event", "ESCALATED").gte("created_at", since))
    return {"chats": chats, "messages": inbound, "after_hours": after_hours, "orders_paid": len(paid),
            "revenue_minor": sum(int(o.get("subtotal_minor") or 0) for o in paid), "bookings": len(bookings), "handoffs": handoffs}


def weekly_text(business: str, cfg, n: dict, now: datetime) -> str:
    local = to_local(now, cfg.timezone)
    head = f"📊 Your Hooze week: {business}\n{(local - timedelta(days=7)):%d %b} – {local:%d %b}\n"
    if not n["messages"]:
        return head + "\nA quiet week: no customer messages came in. Share your WhatsApp number on your status, Instagram bio and receipts to bring in more chats."
    lines = [f"💬 {n['chats']} customer chat{'s' if n['chats'] != 1 else ''}, {n['messages']} message{'s' if n['messages'] != 1 else ''} answered"]
    if cfg.hours:
        lines.append(f"🌙 {n['after_hours']} came in while you were closed")
    if "ordering" in cfg.capabilities:
        lines.append(f"🛒 {n['orders_paid']} paid order{'s' if n['orders_paid'] != 1 else ''} · {fmt_money(n['revenue_minor'], cfg.currency)}")
    if "booking" in cfg.capabilities:
        lines.append(f"📅 {n['bookings']} booking request{'s' if n['bookings'] != 1 else ''}")
    lines.append(f"🙋 {n['handoffs']} passed to your team")
    return head + "\n" + "\n".join(lines)


async def maybe_send_weekly(*, pdb, outbox, events, cfg, tenant: dict, now: datetime, force: bool = False) -> Optional[str]:
    """Monday from 08:00 local: reserve this week's slot, then send. Returns the period sent, or None."""
    local = to_local(now, cfg.timezone)
    if not force and (local.weekday() != 0 or local.hour < 8):
        return None
    period = iso_week(local)
    try:
        await pdb.table("report_log").insert({"tenant_id": tenant["id"], "kind": WEEKLY, "period": period}).execute()
    except Exception as e:
        if is_unique_violation(e):
            return None                                   # already sent this week
        raise
    text = weekly_text(tenant.get("name") or cfg.persona.business_name, cfg, await weekly_numbers(pdb, tenant["id"], cfg, now), now)
    delivered = await outbox.notify_staff(text)
    await (events.info if delivered else events.error)("WEEKLY_REPORT_SENT" if delivered else "WEEKLY_REPORT_FAILED",
                                                       tenant_id=tenant["id"], period=period, delivered=delivered)
    return period

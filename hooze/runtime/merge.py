"""Merge a customer's duplicate conversations (one per WhatsApp number, created before conversations became one-per-customer).

Nothing is lost: every message and booking is MOVED to the surviving conversation first, and only then are the emptied duplicates
removed. If it is interrupted halfway it can simply be run again. Run it as a dry run first.
"""
from __future__ import annotations
from typing import Optional


def _recency(r: dict) -> str:
    return str(r.get("last_inbound_at") or r.get("created_at") or "")


async def merge_contact_conversations(db, contact_id: str, *, dry_run: bool = True) -> Optional[dict]:
    rows = (await db.table("conversations").select("*").eq("contact_id", contact_id).execute()).data or []
    if len(rows) < 2:
        return None
    rows.sort(key=_recency, reverse=True)
    keeper, others = rows[0], rows[1:]
    report = {"contact_id": contact_id, "keeper": keeper["id"], "merged": [o["id"] for o in others],
              "messages_moved": sum(int(o.get("message_count") or 0) for o in others)}
    if dry_run:
        return report
    for o in others:
        await db.table("messages").update({"conversation_id": keeper["id"]}).eq("conversation_id", o["id"]).execute()
        await db.table("bookings").update({"conversation_id": keeper["id"]}).eq("conversation_id", o["id"]).execute()
        await db.table("nudge_state").delete().eq("conversation_id", o["id"]).execute()          # unique per conversation: the keeper's own stays
    # Combine what the keeper remembers. A hand-off anywhere keeps the merged chat handed off (a human was involved: never silently hand it back to the bot).
    older_first = sorted(rows, key=_recency)
    summary = "\n".join(s for s in ((r.get("summary") or "").strip() for r in older_first) if s)
    status = "handoff" if any(r.get("status") == "handoff" for r in rows) else keeper.get("status", "active")
    await db.table("conversations").update({
        "summary": summary, "summary_upto": sum(int(r.get("summary_upto") or 0) for r in rows),
        "message_count": sum(int(r.get("message_count") or 0) for r in rows), "user_message_count": sum(int(r.get("user_message_count") or 0) for r in rows),
        "status": status,
    }).eq("id", keeper["id"]).execute()
    for o in others:
        await db.table("conversations").delete().eq("id", o["id"]).execute()
    return report


async def merge_tenant_conversations(db, *, dry_run: bool = True) -> dict:
    convs = (await db.table("conversations").select("id,contact_id").execute()).data or []
    seen: dict = {}
    for c in convs:
        seen.setdefault(c["contact_id"], []).append(c["id"])
    reports = []
    for contact_id, ids in seen.items():
        if len(ids) > 1:
            r = await merge_contact_conversations(db, contact_id, dry_run=dry_run)
            if r:
                reports.append(r)
    return {"dry_run": dry_run, "customers_with_duplicates": len(reports), "conversations_removed": 0 if dry_run else sum(len(r["merged"]) for r in reports), "details": reports}

"""Conversation persistence (contacts, conversations, messages) on a TenantDB.

Merges pd `leads`/`messages`/`sessions` and savoury `customers`/`conversation_messages`
into one tenant-scoped model. Idempotency is a DB UNIQUE constraint on
(tenant_id, channel_id, provider_message_id) — the claim is an INSERT, so two
concurrent deliveries cannot both win, even across workers.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional

from hooze.kernel.db import TenantDB, first, is_unique_violation


class ConversationStore:
    def __init__(self, db: TenantDB, clock):
        self.db, self.clock = db, clock

    def _now(self) -> str:
        return self.clock().isoformat()

    async def get_or_create_contact(self, phone: str, push_name: Optional[str]) -> tuple[dict, bool]:
        row = first(await self.db.table("contacts").select("*").eq("phone", phone).limit(1).execute())
        if row:
            if push_name and not row.get("display_name"):
                await self.db.table("contacts").update({"display_name": push_name}).eq("id", row["id"]).execute()
                row["display_name"] = push_name
            return row, False
        try:
            ins = await self.db.table("contacts").insert({"phone": phone, "display_name": push_name, "attributes": {}, "ai_paused": False, "opted_out": False}).execute()
            return ins.data[0], True
        except Exception as e:
            if not is_unique_violation(e):
                raise                                   # a real failure must not be disguised as "someone else created it"
            row = first(await self.db.table("contacts").select("*").eq("phone", phone).limit(1).execute())   # lost a race on unique(tenant_id, phone)
            if row is None:
                raise
            return row, False

    async def get_or_create_conversation(self, contact_id: str, channel_id: str, initial_state: str) -> dict:
        """ONE conversation per customer per client, however many WhatsApp numbers the client has. A customer who writes to a second number
        continues the same chat (history, summary, stage, hand-off) instead of starting over; the conversation simply moves to the number
        they wrote to last, so replies, receipts and follow-ups go out from where they are talking.
        Customers who ALREADY have one conversation per number (created before this) keep each as it is until an operator merges them
        (`merge_contact_conversations`); we never guess-merge live history on the hot path."""
        rows = (await self.db.table("conversations").select("*").eq("contact_id", contact_id).execute()).data or []
        for r in rows:
            if r["channel_id"] == channel_id:
                return r
        if rows:
            keeper = max(rows, key=lambda r: str(r.get("last_inbound_at") or r.get("created_at") or ""))
            try:
                await self.db.table("conversations").update({"channel_id": channel_id}).eq("id", keeper["id"]).execute()
                return {**keeper, "channel_id": channel_id}
            except Exception as e:
                if not is_unique_violation(e):
                    raise
                row = first(await self.db.table("conversations").select("*").eq("contact_id", contact_id).eq("channel_id", channel_id).limit(1).execute())
                if row is None:
                    raise
                return row
        try:
            ins = await self.db.table("conversations").insert({
                "contact_id": contact_id, "channel_id": channel_id, "state": initial_state, "status": "active",
                "summary": "", "summary_upto": 0, "user_message_count": 0, "message_count": 0,
            }).execute()
            return ins.data[0]
        except Exception as e:
            if not is_unique_violation(e):
                raise
            row = first(await self.db.table("conversations").select("*").eq("contact_id", contact_id).limit(1).execute())
            if row is None:
                raise
            return row                                  # lost a race on unique(tenant_id, contact_id, channel_id)

    async def claim_inbound(self, conversation: dict, contact_id: str, channel_id: str, provider_message_id: Optional[str], content: str, content_type: str) -> bool:
        """True if this is the first time we've seen this provider message id."""
        try:
            await self.db.table("messages").insert({
                "conversation_id": conversation["id"], "contact_id": contact_id, "channel_id": channel_id,
                "direction": "inbound", "sender_type": "contact", "content": content, "content_type": content_type,
                "provider_message_id": provider_message_id, "meta": {},
            }).execute()
        except Exception as e:
            if is_unique_violation(e):
                return False                            # genuine redelivery of a message we already hold
            raise                                       # anything else is a failure, NOT a duplicate: surface it (TURN_CRASHED) instead of dropping the message
        upd = {"last_inbound_at": self._now(), "message_count": conversation.get("message_count", 0) + 1}
        if content_type == "text":
            upd["user_message_count"] = conversation.get("user_message_count", 0) + 1
        await self.db.table("conversations").update(upd).eq("id", conversation["id"]).execute()
        conversation.update(upd)
        return True

    async def has_reply_after_inbound(self, conversation_id: str, provider_message_id: str) -> bool:
        """Did anyone (bot or human) already answer this inbound message? Used ONLY when a queued message is replayed after a crash:
        the inbound row is stored (so claim_inbound says "duplicate"), but if nothing went out yet the customer is still waiting."""
        m = first(await self.db.table("messages").select("created_at").eq("conversation_id", conversation_id)
                  .eq("provider_message_id", provider_message_id).eq("direction", "inbound").limit(1).execute())
        if not m:
            return False
        out = (await self.db.table("messages").select("id").eq("conversation_id", conversation_id).eq("direction", "outbound")
               .in_("sender_type", ["bot", "human"]).gte("created_at", m["created_at"]).limit(1).execute()).data or []
        return bool(out)

    async def record_outbound(self, conversation_id: str, content: str, sender_type: str = "bot", meta: Optional[dict] = None) -> None:
        await self.db.table("messages").insert({
            "conversation_id": conversation_id, "direction": "outbound", "sender_type": sender_type,
            "content": content, "content_type": "text", "meta": meta or {},
        }).execute()
        await self.db.table("conversations").update({"last_outbound_at": self._now()}).eq("id", conversation_id).execute()

    async def recent_messages(self, conversation_id: str, limit: int) -> list[dict]:
        rows = (await self.db.table("messages").select("sender_type, content, created_at, meta")
                .eq("conversation_id", conversation_id).order("created_at", desc=True).limit(limit).execute()).data or []
        return list(reversed(rows))

    async def count_recent_bot(self, conversation_id: str, seconds: int = 60) -> int:
        cutoff = (self.clock() - timedelta(seconds=seconds)).isoformat()
        rows = (await self.db.table("messages").select("id").eq("conversation_id", conversation_id)
                .eq("sender_type", "bot").gte("created_at", cutoff).execute()).data or []
        return len(rows)

    async def set_contact(self, contact_id: str, **fields) -> None:
        await self.db.table("contacts").update(fields).eq("id", contact_id).execute()

    async def set_conversation(self, conversation_id: str, **fields) -> None:
        await self.db.table("conversations").update(fields).eq("id", conversation_id).execute()

    async def merge_slots(self, contact: dict, slots: dict) -> dict:
        attrs = dict(contact.get("attributes") or {})
        merged = {**(attrs.get("slots") or {}), **slots}
        attrs["slots"] = merged
        await self.set_contact(contact["id"], attributes=attrs)
        contact["attributes"] = attrs
        return merged

    async def count_messages(self, conversation_id: str) -> int:
        res = await self.db.table("messages").select("id", count="exact").eq("conversation_id", conversation_id).limit(1).execute()
        return res.count or 0

    async def messages_range(self, conversation_id: str, start: int, end_inclusive: int) -> list[dict]:
        return (await self.db.table("messages").select("sender_type, content").eq("conversation_id", conversation_id)
                .order("created_at").range(start, end_inclusive).execute()).data or []

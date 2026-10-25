"""Out-of-turn sends: nudges, booking confirmations, payment receipts, staff alerts.

pd had `notifications.send_alert` (hard-wired to "you + the client team" numbers)
and savoury had `notify_new_paid_order` (loops `settings.admin_number_list`).
Here staff numbers are per-tenant config and the channel is resolved per tenant.
"""
from __future__ import annotations
from datetime import datetime, timezone

from hooze.kernel.db import first
from hooze.runtime.store import ConversationStore


class Outbox:
    def __init__(self, platform_db, adapters, tenant_id: str, staff_numbers: list[str], events, clock=None):
        self.pdb, self.adapters, self.tenant_id, self.staff, self.events = platform_db, adapters, tenant_id, staff_numbers, events
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _channel(self, channel_id: str | None = None) -> dict | None:
        q = self.pdb.table("channels").select("*").eq("tenant_id", self.tenant_id).eq("status", "active")
        if channel_id:
            q = q.eq("id", channel_id)
        return first(await q.limit(1).execute())

    async def _record(self, ch: dict, phone: str, text: str, conversation_id: str | None, store) -> None:
        """Bookkeeping AFTER a confirmed send (never retried). Without this, nudges / booking confirmations / receipts were
        sent but absent from the transcript, so the next turn's LLM had no idea what the customer was replying to."""
        try:
            store = store or ConversationStore(self.pdb.for_tenant(self.tenant_id), self.clock)
            if not conversation_id:
                digits = "".join(c for c in phone if c.isdigit())
                contact = first(await store.db.table("contacts").select("id").eq("phone", digits).limit(1).execute())
                conv = contact and first(await store.db.table("conversations").select("id").eq("contact_id", contact["id"]).eq("channel_id", ch["id"]).limit(1).execute())
                if not conv:
                    return                              # not a customer (or no conversation yet): nothing to attach it to
                conversation_id = conv["id"]
            await store.record_outbound(conversation_id, text, "bot")
        except Exception as e:
            await self.events.warn("OUTBOUND_RECORD_FAILED", tenant_id=self.tenant_id, error=str(e)[:160])

    async def send(self, phone: str, text: str, channel_id: str | None = None, *, conversation_id: str | None = None, store=None, record: bool = True) -> bool:
        if not channel_id and conversation_id:
            # A tenant can have several numbers: answer from the one this conversation is on, not just "the first active one".
            conv = first(await self.pdb.for_tenant(self.tenant_id).table("conversations").select("channel_id").eq("id", conversation_id).limit(1).execute())
            channel_id = conv["channel_id"] if conv else None
        ch = await self._channel(channel_id)
        if not ch and channel_id:
            ch = await self._channel(None)                      # that number was disabled since: fall back to any active one
        if not ch:
            await self.events.warn("OUTBOX_NO_CHANNEL", tenant_id=self.tenant_id)
            return False
        await self.adapters.for_channel(ch).send_text(phone, text, typing_delay=0.0)   # network call only is retried
        if record:
            await self._record(ch, phone, text, conversation_id, store)
        return True

    async def notify_staff(self, text: str) -> int:
        n = 0
        if not self.staff:
            await self.events.warn("NO_STAFF_NUMBER_CONFIGURED", tenant_id=self.tenant_id)
            return 0
        channels = (await self.pdb.table("channels").select("*").eq("tenant_id", self.tenant_id).eq("status", "active").order("created_at").execute()).data or []
        for phone in self.staff:
            # A client can have several numbers. Alerts used to go out through whichever came back first, so one disconnected/banned number
            # silently swallowed every alert. Try each active number in a stable order until one delivers.
            sent, last = False, ""
            for ch in channels:
                try:
                    await self.adapters.for_channel(ch).send_text(phone, text, typing_delay=0.0)   # staff alerts never go into a customer transcript
                    sent = True
                    break
                except Exception as e:
                    last = f"{ch.get('instance_name')}: {str(e)[:120]}"
            if sent:
                n += 1
            else:
                await self.events.error("STAFF_ALERT_FAILED", tenant_id=self.tenant_id, error=last or "no active channel", tried=len(channels))
        return n

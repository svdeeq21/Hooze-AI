"""Messenger: the ONE outbound boundary inside a turn.

Order matters (audit H3): the network send happens first and is the only retried
step (inside the adapter); recording to the DB happens AFTER a confirmed send and
is never allowed to trigger a resend. Formatting normalisation is applied by the
adapter at this same boundary.
"""
from __future__ import annotations
import asyncio, random
from typing import Optional


class Messenger:
    def __init__(self, *, adapter, store, to: str, conversation_id: str, tenant_id: str, events, trace_id: str,
                 delay_range: tuple[float, float] = (1.0, 2.5), sleep=asyncio.sleep):
        self.adapter, self.store, self.to, self.conversation_id = adapter, store, to, conversation_id
        self.tenant_id, self.events, self.trace_id = tenant_id, events, trace_id
        self.delay_range, self._sleep = delay_range, sleep
        self.sent: list[str] = []

    async def text(self, text: str, *, meta: Optional[dict] = None, typing: bool = True) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        delay = random.uniform(*self.delay_range) if typing and self.delay_range[1] > 0 else 0.0
        # ONE wait, owned by the provider: Evolution shows the "typing…" presence for `delay` and then sends. This used to ALSO sleep here for
        # the same time first, so every message waited twice (the real logs: 3.5-7s per send) and the first wait showed no typing at all.
        await self.adapter.send_text(self.to, text, typing_delay=delay)      # retried inside the adapter
        self.sent.append(text)
        try:                                                                  # bookkeeping: never retried
            await self.store.record_outbound(self.conversation_id, text, "bot", meta)
        except Exception as e:
            await self.events.warn("OUTBOUND_RECORD_FAILED", tenant_id=self.tenant_id, trace_id=self.trace_id, error=str(e)[:160])
        return True

    async def hold(self, text: str) -> bool:
        """A holding message ("one moment…"). Recorded as `system`, NOT `bot`: it must never count as the answer, or a replay after an
        outage would see "already replied" and never send the real one."""
        await self.adapter.send_text(self.to, text, typing_delay=0.0)
        self.sent.append(text)
        try:
            await self.store.record_outbound(self.conversation_id, text, "system")
        except Exception as e:
            await self.events.warn("OUTBOUND_RECORD_FAILED", tenant_id=self.tenant_id, trace_id=self.trace_id, error=str(e)[:160])
        return True

    async def media(self, url: str, media_type: str, caption: str = "") -> bool:
        await self.adapter.send_media(self.to, url, media_type, caption)
        try:
            await self.store.record_outbound(self.conversation_id, f"[{media_type}] {caption}".strip(), "bot", {"media_url": url})
        except Exception:
            pass
        return True

"""Availability: "OPEN != ACCEPTING ORDERS". Three layers, first match wins:
manual PAUSED > manual CLOSED > operating hours.

Extracted from: savoury `availability.py` (pure functions, kept) + `availability_store.py`
+ admin pause/resume commands. Generalised: any tenant can use it (a clinic's booking
hours, a shop's opening hours); real-estate tenants simply don't enable it.
Change: timezone is an IANA name from config (`Africa/Lagos`), not a hand-maintained UTC
offset that is silently wrong the day a client is outside WAT.
"""
from __future__ import annotations
import re
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

from hooze.capabilities.base import Capability, GateResult, StaffCommand, register
from hooze.kernel.db import first


def to_local(now_utc: datetime, tz_name: str) -> datetime:
    return now_utc.astimezone(ZoneInfo(tz_name))


def within_hours(window: Optional[list[str]], local_now: datetime) -> bool:
    if not window:
        return False
    o, c = dtime.fromisoformat(window[0]), dtime.fromisoformat(window[1])
    cur = local_now.time().replace(tzinfo=None)
    return (o <= cur <= c) if o <= c else (cur >= o or cur <= c)     # handles overnight (18:00-02:00)


def resolve_open(status_row: Optional[dict], hours: dict, local_now: datetime) -> tuple[bool, Optional[str]]:
    status = (status_row or {}).get("status", "OPEN")
    if status == "PAUSED":
        return False, (status_row or {}).get("pause_message") or "We're not taking orders at the moment. Please check back a little later."
    if status == "CLOSED":
        return False, "We're closed right now. Please check back during our opening hours."
    if hours:                                                          # no hours configured => open 24/7
        day = str(local_now.weekday())
        window = hours[day] if day in hours else hours.get("default")
        if not within_hours(window, local_now):
            return False, "We're outside our opening hours right now. Please check back later."
    return True, None


@register
class AvailabilityCapability(Capability):
    name = "availability"

    async def _row(self) -> Optional[dict]:
        return first(await self.ctx.db.table("availability").select("*").limit(1).execute())

    async def is_open(self) -> tuple[bool, Optional[str]]:
        local = to_local(self.ctx.clock(), self.ctx.config.timezone)
        return resolve_open(await self._row(), self.config.get("hours", {}), local)

    async def gate(self, turn) -> Optional[GateResult]:
        ok, reason = await self.is_open()
        return None if ok else GateResult(reply=reason, dedupe=True)    # dedupe: never repeat the closed notice (real transcript bug)

    async def _set(self, status: str, reason: Optional[str] = None) -> None:
        fields = {"status": status, "pause_reason": reason, "pause_message": None, "updated_at": self.ctx.clock().isoformat()}
        if await self._row():
            await self.ctx.db.table("availability").update(fields).execute()
        else:
            await self.ctx.db.table("availability").insert(fields).execute()

    def staff_commands(self) -> list[StaffCommand]:
        async def pause(m, actor):
            reason = (m.group("reason") or "").strip() or None
            await self._set("PAUSED", reason)
            await self.ctx.events.info("AVAILABILITY_PAUSED", tenant_id=self.ctx.tenant_id, actor=actor[:6] + "****", reason=reason)
            return "⏸️ Orders paused." + (f" Reason: {reason}" if reason else "") + " Say \"resume\" to reopen."
        async def resume(m, actor):
            await self._set("OPEN")
            await self.ctx.events.info("AVAILABILITY_RESUMED", tenant_id=self.ctx.tenant_id, actor=actor[:6] + "****")
            return "▶️ Orders resumed. (Opening hours still apply.)"
        async def close(m, actor):
            await self._set("CLOSED")
            return "🔒 Marked closed. Say \"reopen\" when ready."
        async def status(m, actor):
            ok, reason = await self.is_open()
            return "✅ Accepting orders." if ok else f"⛔ Not accepting orders: {reason}"
        return [
            StaffCommand("pause orders [reason]", [r"^(?:pause|stop taking)(?: orders)?\s*(?:[,:\-]\s*(?P<reason>.*))?$"], pause),
            StaffCommand("resume / reopen", [r"^(?:resume|unpause|reopen|open)(?: orders)?\s*$"], resume),
            StaffCommand("close", [r"^close(?:d)?\s*$"], close),
            StaffCommand("status", [r"^status\s*$", r"^how (?:are|is) (?:we|it|things)\b.*$"], status),
        ]

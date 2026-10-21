"""Background upkeep that runs inside the API process: recover interrupted messages, watch WhatsApp connections, purge old queue rows.

Safe with more than one worker for the queue (DB leases), but the connection monitor and alert de-duplication are per-process, so
run ONE worker until shared state (Upstash) is configured; the same advice the platform already gives for locks.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Optional

from hooze.kernel.errors import describe_exc

_log = logging.getLogger("hooze")


def is_open(state: str) -> bool:
    return (state or "").lower() in ("open", "connected")


async def check_channels(pdb, adapters, events, down: dict, now: float = 0.0, reemit_s: int = 1800) -> dict:
    """Report an active client's WhatsApp number that is not connected, and note when it recovers.

    `down` maps channel id -> when it was last reported. A number that stays down is re-reported at most every `reemit_s` (the first
    real run wrote an ERROR row every 2 minutes per dead number = ~720 rows/day each). Clients that are not live (onboarding/suspended)
    are skipped: a number that was never linked yet is not an incident.
    """
    tenants = {t["id"]: t["status"] for t in ((await pdb.table("tenants").select("id, status").execute()).data or [])}
    channels = [c for c in ((await pdb.table("channels").select("*").eq("status", "active").execute()).data or []) if tenants.get(c["tenant_id"]) == "active"]

    async def one(ch: dict) -> tuple[dict, str]:
        try:
            return ch, await asyncio.wait_for(adapters.for_channel(ch).connection_status(), 8.0)
        except Exception:
            return ch, "UNREACHABLE"
    out = {"checked": len(channels), "down": 0}
    for ch, state in await asyncio.gather(*[one(c) for c in channels]):
        if is_open(state):
            if ch["id"] in down:
                down.pop(ch["id"])
                await events.info("CHANNEL_RECOVERED", tenant_id=ch["tenant_id"], channel_id=ch["id"])
        else:
            out["down"] += 1
            if ch["id"] not in down or now - down[ch["id"]] >= reemit_s:
                down[ch["id"]] = now
                await events.error("CHANNEL_DISCONNECTED", tenant_id=ch["tenant_id"], channel_id=ch["id"], state=state, instance=ch["instance_name"])
    return out


class Maintenance:
    def __init__(self, inbox, pdb, adapters, events, settings, clock):
        self.inbox, self.pdb, self.adapters, self.events, self.s, self.clock = inbox, pdb, adapters, events, settings, clock
        self._down: dict = {}
        self._last_channel_check: Optional[float] = None
        self._last_purge: Optional[float] = None
        self.startup_delay = 3.0

    async def run_once(self) -> dict:
        """One pass. Each step is isolated: a failure in one never stops the others (and is logged, never raised)."""
        now = self.clock().timestamp()
        out: dict = {}
        for name, fn in (("inbound", self.inbox.recover),):
            try:
                out[name] = await fn()
            except Exception as e:
                await self.events.error("MAINTENANCE_STEP_FAILED", step=name, error=describe_exc(e))
        if self._last_channel_check is None or now - self._last_channel_check >= self.s.channel_check_interval_seconds:
            self._last_channel_check = now
            try:
                out["channels"] = await check_channels(self.pdb, self.adapters, self.events, self._down, now, self.s.channel_reemit_seconds)
            except Exception as e:
                await self.events.error("MAINTENANCE_STEP_FAILED", step="channels", error=describe_exc(e))
        if self._last_purge is None or now - self._last_purge >= 3600:
            self._last_purge = now
            try:
                out["purged"] = await self.inbox.purge()
            except Exception as e:
                await self.events.error("MAINTENANCE_STEP_FAILED", step="purge", error=describe_exc(e))
        return out

    async def loop(self) -> None:
        await asyncio.sleep(self.startup_delay)                 # let the app finish booting; then immediately pick up anything a previous process left behind
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log.warning(f"maintenance pass failed: {e}")
            await asyncio.sleep(self.s.inbound_recovery_interval_seconds)

"""Durable inbound queue.

Flow:  webhook (authenticated) -> enqueue (one INSERT, deduped by provider id) -> acknowledge -> process in the background.
       Anything not finished (process died, deploy, DB blip, a crash inside the turn) is re-claimed by `recover()` and run again.

Guarantees and their limits (stated plainly so nobody assumes more):
  * AT-LEAST-ONCE processing of every accepted message. The only way to lose one is the database being down at the instant the webhook
    arrives: then the webhook returns 503 and the provider's own retry (if any) applies.
  * AT-MOST-ONCE reply per message in the normal case: a replay only re-runs the turn if NOTHING was sent in answer to it yet
    (pipeline replay guard). If a crash happens halfway through a multi-message reply, that partial reply stands.
  * Claims are single atomic statements with a lease (migrations/0003), so two workers never run the same row concurrently.
"""
from __future__ import annotations
import asyncio
from typing import Optional

from hooze.kernel.db import first, is_unique_violation
from hooze.kernel.errors import describe_exc


class Inbox:
    def __init__(self, pdb, pipeline, adapters, events, settings, clock):
        self.pdb, self.pipeline, self.adapters, self.events, self.s, self.clock = pdb, pipeline, adapters, events, settings, clock
        self._sem = asyncio.Semaphore(max(1, settings.inbound_max_concurrency))

    # ── enqueue (called by the webhook BEFORE it acknowledges) ───────────
    async def enqueue(self, channel: dict, body: dict, provider_message_id: Optional[str]) -> tuple[Optional[str], bool]:
        """(event_id, is_new). A redelivery of a message we already hold returns (None, False). Any other DB error propagates so the
        webhook answers 5xx instead of acknowledging a message it did not save."""
        try:
            res = await self.pdb.table("inbound_events").insert({"tenant_id": channel["tenant_id"], "channel_id": channel["id"],
                                                                 "provider_message_id": provider_message_id, "payload": body}).execute()
        except Exception as e:
            if is_unique_violation(e):
                return None, False
            raise
        return res.data[0]["id"], True

    # ── processing ───────────────────────────────────────────────────────
    async def process(self, event_id: str) -> str:
        """Claim and run ONE row (the webhook's own background task). Never raises."""
        try:
            row = first(await self.pdb.rpc("claim_inbound_event", {"p_id": event_id, "p_lease_seconds": self.s.inbound_lease_seconds}).execute())
            return await self._run(row) if row else "skipped"
        except Exception as e:
            await self.events.error("INBOUND_PROCESS_ERROR", event_id=event_id, error=describe_exc(e))
            return "error"

    async def recover(self, limit: int = 20) -> dict:
        """Claim rows nobody is working on (died worker / unprocessed after the grace period) and run them."""
        rows = (await self.pdb.rpc("claim_stale_inbound_events", {"p_limit": limit, "p_lease_seconds": self.s.inbound_lease_seconds,
                                                                  "p_min_age_seconds": self.s.inbound_recovery_min_age_seconds}).execute()).data or []
        results = await asyncio.gather(*[self._run(r) for r in rows], return_exceptions=True)
        out = {"claimed": len(rows), "done": sum(1 for r in results if r == "done"), "retry": sum(1 for r in results if r == "retry"), "failed": sum(1 for r in results if r == "failed")}
        if rows:
            await self.events.info("INBOUND_RECOVERY", **out)
        return out

    async def _run(self, row: dict) -> str:
        async with self._sem:
            attempt = row["attempts"]
            tid = row["tenant_id"]
            try:
                channel = first(await self.pdb.table("channels").select("*").eq("id", row["channel_id"]).limit(1).execute())
                if not channel or channel["status"] != "active":
                    return await self._finish(row, "done", "channel_inactive")
                inbound = self.adapters.for_channel(channel).parse(row["payload"])
                if inbound is None:
                    return await self._finish(row, "done", "unparseable")
                outcome = await self.pipeline.handle_inbound(channel, inbound, replay=attempt > 1, attempt=attempt)
                if getattr(outcome, "status", None) == "error":           # pipeline already logged TURN_CRASHED; this is a retryable failure
                    raise RuntimeError(getattr(outcome, "detail", "") or "turn crashed")
                return await self._finish(row, "done", None)
            except Exception as e:
                msg = describe_exc(e, 300)
                if attempt >= self.s.inbound_max_attempts:
                    await self.pdb.table("inbound_events").update({"status": "failed", "last_error": msg, "locked_until": None, "finished_at": self.clock().isoformat()}).eq("id", row["id"]).execute()
                    await self.events.error("INBOUND_FAILED", tenant_id=tid, ref_type="inbound_event", ref_id=row["id"], attempts=attempt, error=msg)
                    return "failed"
                from datetime import timedelta
                backoff = min(30 * 2 ** (attempt - 1), 600)                  # 30s, 60s, 120s, 240s ... a transient outage must not burn every attempt in two minutes
                await self.pdb.table("inbound_events").update({"status": "pending", "last_error": msg,
                                                               "locked_until": (self.clock() + timedelta(seconds=backoff)).isoformat()}).eq("id", row["id"]).execute()
                await self.events.warn("INBOUND_RETRY", tenant_id=tid, ref_type="inbound_event", ref_id=row["id"], attempt=attempt, error=msg)
                return "retry"

    async def _finish(self, row: dict, status: str, note: Optional[str]) -> str:
        await self.pdb.table("inbound_events").update({"status": status, "last_error": note, "locked_until": None, "finished_at": self.clock().isoformat()}).eq("id", row["id"]).execute()
        return "done"

    # ── housekeeping / visibility ───────────────────────────────────────
    async def purge(self) -> int:
        """Delete finished rows older than the retention window (the customer's message itself lives on in `messages`)."""
        from datetime import timedelta
        cutoff = (self.clock() - timedelta(days=self.s.inbound_retention_days)).isoformat()
        rows = (await self.pdb.table("inbound_events").delete().eq("status", "done").lt("finished_at", cutoff).execute()).data or []
        return len(rows)

    async def counts(self) -> dict:
        async def n(status):
            return int((await self.pdb.table("inbound_events").select("id", count="exact").eq("status", status).limit(1).execute()).count or 0)
        p, q, f = await asyncio.gather(n("pending"), n("processing"), n("failed"))
        return {"pending": p, "processing": q, "failed": f}

    async def retry(self, event_id: str) -> bool:
        """Operator action: put a FAILED row back in the queue with a fresh attempt budget."""
        rows = (await self.pdb.table("inbound_events").update({"status": "pending", "attempts": 0, "locked_until": None, "last_error": None, "finished_at": None})
                .eq("id", event_id).eq("status", "failed").execute()).data
        return bool(rows)

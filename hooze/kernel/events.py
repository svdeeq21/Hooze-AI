"""Structured events + audit trail + per-turn timings.

Merges pd `AuditLogger` (lead_id column) and savoury `AuditLogger`
(ref_type/ref_id) into one tenant-aware bus. Sinks are pluggable so tests use
an in-memory sink and production uses stdout + the `events` table.
A sink failure NEVER propagates (both old loggers had this rule; kept).
"""
from __future__ import annotations
import json, logging, sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from hooze.kernel import capture

_log = logging.getLogger("hooze")
if not _log.handlers:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")

Sink = Callable[[dict], Awaitable[None]]


class MemorySink:
    def __init__(self):
        self.events: list[dict] = []

    async def __call__(self, e: dict) -> None:
        self.events.append(e)

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]

    def find(self, name: str) -> list[dict]:
        return [e for e in self.events if e["event"] == name]


async def stdout_sink(e: dict) -> None:
    level = getattr(logging, e["severity"], logging.INFO)
    _log.log(level, json.dumps(e, default=str))


def db_sink(platform_db) -> Sink:
    async def _sink(e: dict) -> None:
        await platform_db.table("events").insert({
            "tenant_id": e.get("tenant_id"), "event": e["event"], "severity": e["severity"],
            "ref_type": e.get("ref_type"), "ref_id": e.get("ref_id"),
            "trace_id": e.get("trace_id"), "metadata": e.get("metadata") or {},
        }).execute()
    return _sink


class BufferedInserter:
    """Writes rows to a table in BATCHES, in the background, instead of one request per row on the customer's time.

    Every turn used to wait for ~5 separate database inserts (one per event, plus one per AI call for metering) before the next step could
    run. Measured with scripts/load_sim.py that is a large share of a turn's database time. Now rows are queued and flushed every second
    (or as soon as `max_batch` are waiting) in one request. Trade-off, deliberately accepted: if the process is killed, at most ~1 second
    of LOG rows is lost; customer messages, orders and payments are never written this way. If the database is down the buffer is
    capped (oldest rows dropped) so memory can't grow without limit."""
    def __init__(self, platform_db, table: str, *, flush_every: float = 1.0, max_batch: int = 200, max_buffer: int = 5000):
        self.pdb, self.table, self.flush_every, self.max_batch, self.max_buffer = platform_db, table, flush_every, max_batch, max_buffer
        self._buf: list[dict] = []
        self._task = None
        self.dropped = 0

    def add(self, row: dict) -> None:
        import asyncio
        self._buf.append(row)
        if len(self._buf) > self.max_buffer:
            over = len(self._buf) - self.max_buffer
            del self._buf[:over]
            self.dropped += over
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._run())
            except RuntimeError:                   # no running loop (scripts/tests at import time): flushed on the next add/flush()
                self._task = None

    async def _run(self) -> None:
        import asyncio
        while self._buf:
            await asyncio.sleep(self.flush_every if len(self._buf) < self.max_batch else 0)
            await self.flush()

    async def flush(self) -> int:
        n = 0
        while self._buf:
            batch, self._buf = self._buf[: self.max_batch], self._buf[self.max_batch:]
            try:
                await self.pdb.table(self.table).insert(batch).execute()
                n += len(batch)
                self._fails = 0
            except Exception as exc:              # never break the app over a log write
                self._fails = getattr(self, "_fails", 0) + 1
                if self._fails >= 5:              # a batch the database keeps refusing (bad row, missing table): drop it, don't loop forever
                    self.dropped += len(batch)
                    self._fails = 0
                    _log.warning(f"buffered insert into {self.table} failed 5 times; dropped {len(batch)} rows: {exc}")
                    continue
                self._buf = batch + self._buf     # keep them for the next attempt (the buffer stays bounded by max_buffer)
                _log.warning(f"buffered insert into {self.table} failed ({len(batch)} rows kept for retry): {exc}")
                return n
        return n


def buffered_db_sink(writer: BufferedInserter) -> Sink:
    async def _sink(e: dict) -> None:
        writer.add({"tenant_id": e.get("tenant_id"), "event": e["event"], "severity": e["severity"], "ref_type": e.get("ref_type"),
                    "ref_id": e.get("ref_id"), "trace_id": e.get("trace_id"), "metadata": e.get("metadata") or {}})
    return _sink


class EventBus:
    def __init__(self, sinks: Optional[list[Sink]] = None):
        self.sinks: list[Sink] = sinks if sinks is not None else [stdout_sink]

    async def emit(self, event: str, *, tenant_id: Optional[str] = None, severity: str = "INFO",
                   ref_type: Optional[str] = None, ref_id: Optional[str] = None,
                   trace_id: Optional[str] = None, **metadata: Any) -> None:
        e = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "severity": severity,
             "tenant_id": tenant_id, "ref_type": ref_type, "ref_id": ref_id and str(ref_id),
             "trace_id": trace_id, "metadata": metadata}
        cap = capture.active()
        if cap is not None:
            cap["events"].append(e)                 # the web test chat shows the turn's steps to the person testing it
        for sink in self.sinks:
            try:
                await sink(e)
            except Exception as exc:  # logging must never break a customer's turn
                _log.warning(f"event sink failed: {exc}")

    async def info(self, event, **kw): await self.emit(event, severity="INFO", **kw)
    async def warn(self, event, **kw): await self.emit(event, severity="WARNING", **kw)
    async def error(self, event, **kw): await self.emit(event, severity="ERROR", **kw)

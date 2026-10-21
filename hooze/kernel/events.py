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


class EventBus:
    def __init__(self, sinks: Optional[list[Sink]] = None):
        self.sinks: list[Sink] = sinks if sinks is not None else [stdout_sink]

    async def emit(self, event: str, *, tenant_id: Optional[str] = None, severity: str = "INFO",
                   ref_type: Optional[str] = None, ref_id: Optional[str] = None,
                   trace_id: Optional[str] = None, **metadata: Any) -> None:
        e = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, "severity": severity,
             "tenant_id": tenant_id, "ref_type": ref_type, "ref_id": ref_id and str(ref_id),
             "trace_id": trace_id, "metadata": metadata}
        for sink in self.sinks:
            try:
                await sink(e)
            except Exception as exc:  # logging must never break a customer's turn
                _log.warning(f"event sink failed: {exc}")

    async def info(self, event, **kw): await self.emit(event, severity="INFO", **kw)
    async def warn(self, event, **kw): await self.emit(event, severity="WARNING", **kw)
    async def error(self, event, **kw): await self.emit(event, severity="ERROR", **kw)

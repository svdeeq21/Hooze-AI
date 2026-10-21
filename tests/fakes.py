"""Test doubles: in-memory Supabase, scriptable LLM, recording channel adapter, fake payment provider.

The fake DB enforces the SAME unique / partial-unique constraints as migrations/0001 (idempotency index,
one-CART-per-contact, one-PAYMENT_PENDING-per-contact, unique payment reference, one active config, ...),
so tests exercise the invariants the application relies on instead of a fake that accepts anything.
"""
from __future__ import annotations
import math, re, uuid, zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from hooze.channels.base import InboundMessage
from hooze.llm.gateway import InterpretResult, LLMUnavailable
from hooze.runtime.models import Action, Interpretation

UNIQUES: dict[str, list[tuple[tuple, Optional[Callable]]]] = {
    "contacts": [(("tenant_id", "phone"), None)],
    "conversations": [(("tenant_id", "contact_id", "channel_id"), None)],
    "messages": [(("tenant_id", "channel_id", "provider_message_id"), lambda r: r.get("provider_message_id") is not None)],
    "payments": [(("reference",), None)],
    "orders": [(("tenant_id", "contact_id"), lambda r: r.get("status") == "CART"),
               (("tenant_id", "contact_id"), lambda r: r.get("status") == "PAYMENT_PENDING")],
    "nudge_state": [(("conversation_id",), None)],
    "knowledge_items": [(("tenant_id", "source", "external_id"), None)],
    "tenant_configs": [(("tenant_id", "version"), None), (("tenant_id",), lambda r: r.get("status") == "active")],
    "channels": [(("kind", "instance_name"), None)],
    "availability": [(("tenant_id",), None)],
    "inbound_events": [(("channel_id", "provider_message_id"), lambda r: r.get("provider_message_id") is not None)],
}


class Clock:
    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc)   # 12:00 in Africa/Lagos
    def __call__(self) -> datetime: return self.now
    def advance(self, **kw) -> None: self.now += timedelta(**kw)


class Result:
    def __init__(self, data, count=None): self.data, self.count = data, count


class FakeQuery:
    def __init__(self, db: "FakeDB", table: str):
        self.db, self.t = db, table
        self._op, self._payload, self._filters = "select", None, []
        self._order, self._limit, self._range, self._count = None, None, None, False

    def select(self, cols="*", count=None, **_): self._op, self._count = "select", count == "exact"; return self
    def insert(self, payload, **_): self._op, self._payload = "insert", payload; return self
    def update(self, payload, **_): self._op, self._payload = "update", payload; return self
    def delete(self, *_, **__): self._op = "delete"; return self
    def eq(self, c, v): self._filters.append((c, "eq", v)); return self
    def neq(self, c, v): self._filters.append((c, "neq", v)); return self
    def in_(self, c, v): self._filters.append((c, "in", list(v))); return self
    def lt(self, c, v): self._filters.append((c, "lt", v)); return self
    def lte(self, c, v): self._filters.append((c, "lte", v)); return self
    def gt(self, c, v): self._filters.append((c, "gt", v)); return self
    def gte(self, c, v): self._filters.append((c, "gte", v)); return self
    def order(self, col, desc=False): self._order = (col, desc); return self
    def limit(self, n): self._limit = n; return self
    def range(self, a, b): self._range = (a, b); return self

    def _match(self, row) -> bool:
        for c, op, v in self._filters:
            x = row.get(c)
            if op == "eq" and x != v: return False
            if op == "neq" and x == v: return False
            if op == "in" and x not in v: return False
            if op in ("lt", "lte", "gt", "gte"):
                if x is None: return False
                x, v2 = str(x), str(v)
                if (op == "lt" and not x < v2) or (op == "lte" and not x <= v2) or (op == "gt" and not x > v2) or (op == "gte" and not x >= v2): return False
        return True

    async def execute(self) -> Result:
        rows = self.db.tables.setdefault(self.t, [])
        if self._op == "insert":
            payload = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for p in payload:
                r = dict(p)
                r.setdefault("id", str(uuid.uuid4()))
                ts = self.db.stamp()
                if self.t == "inbound_events":                       # column defaults from migrations/0003
                    for k, v in {"status": "pending", "attempts": 0, "locked_until": None, "last_error": None, "finished_at": None, "received_at": ts}.items():
                        r.setdefault(k, v)
                r.setdefault("created_at", ts); r.setdefault("updated_at", ts)
                self.db.check_unique(self.t, rows + [r], r)
                rows.append(r); out.append(dict(r))
            return Result(out)
        if self._op == "update":
            hit = [r for r in rows if self._match(r)]
            for r in hit:
                new = {**r, **self._payload}
                self.db.check_unique(self.t, [x if x is not r else new for x in rows], new)
            for r in hit: r.update(self._payload)
            return Result([dict(r) for r in hit])
        if self._op == "delete":
            gone = [r for r in rows if self._match(r)]
            rows[:] = [r for r in rows if not self._match(r)]
            return Result([dict(r) for r in gone])
        out = [dict(r) for r in rows if self._match(r)]
        total = len(out)
        if self._order:
            col, desc = self._order
            out.sort(key=lambda r: (r.get(col) is None, str(r.get(col))), reverse=desc)
        if self._range: out = out[self._range[0]: self._range[1] + 1]
        if self._limit is not None: out = out[: self._limit]
        return Result(out, count=total if self._count else None)


class FakeRPC:
    def __init__(self, db, name, params): self.db, self.name, self.params = db, name, params
    async def execute(self) -> Result: return Result(getattr(self.db, f"rpc_{self.name}")(self.params))


class FakeDB:
    def __init__(self, clock: Clock):
        self.tables: dict[str, list[dict]] = {}
        self.clock, self._n = clock, 0

    def stamp(self) -> str:
        self._n += 1
        return (self.clock() + timedelta(microseconds=self._n)).isoformat()     # strictly increasing, tracks the test clock

    def table(self, name): return FakeQuery(self, name)
    def rpc(self, name, params): return FakeRPC(self, name, params)
    def rows(self, table: str, **where) -> list[dict]:
        return [r for r in self.tables.get(table, []) if all(r.get(k) == v for k, v in where.items())]

    def check_unique(self, table, all_rows, candidate):
        for cols, pred in UNIQUES.get(table, []):
            if pred and not pred(candidate): continue
            if any(candidate.get(c) is None for c in cols): continue
            same = [r for r in all_rows if r is not candidate and all(r.get(c) == candidate.get(c) for c in cols) and (not pred or pred(r))]
            if same: raise Exception(f"duplicate key value violates unique constraint on {table}{cols}")

    # RPC implementations mirror the SQL functions
    def rpc_match_knowledge(self, p):
        q, out = p["p_embedding"], []
        for k in self.tables.get("knowledge_items", []):
            if k["tenant_id"] != p["p_tenant_id"] or not k.get("is_active") or not k.get("embedding"): continue
            if p.get("p_kind") and k["kind"] != p["p_kind"]: continue
            sim = cosine(q, k["embedding"])
            if sim >= p["p_threshold"]: out.append({**{c: k[c] for c in ("id", "kind", "title", "content", "data", "media")}, "similarity": sim})
        return sorted(out, key=lambda r: -r["similarity"])[: p["p_count"]]

    def _lease(self, seconds): return (self.clock() + timedelta(seconds=seconds)).isoformat()

    def rpc_claim_inbound_event(self, p):
        """Mirrors migrations/0003: claim one row atomically (pending, or processing with an expired lease)."""
        now = self.clock().isoformat()
        for r in self.tables.get("inbound_events", []):
            if r["id"] == p["p_id"] and (r["status"] == "pending" or (r["status"] == "processing" and (r["locked_until"] or "") < now)):
                r.update(status="processing", attempts=r["attempts"] + 1, locked_until=self._lease(p["p_lease_seconds"]))
                return [dict(r)]
        return []

    def rpc_claim_stale_inbound_events(self, p):
        now = self.clock().isoformat()
        min_age = (self.clock() - timedelta(seconds=p["p_min_age_seconds"])).isoformat()
        rows = sorted((r for r in self.tables.get("inbound_events", []) if
                       (r["status"] == "pending" and r["received_at"] < min_age and (r["locked_until"] is None or r["locked_until"] < now)) or
                       (r["status"] == "processing" and (r["locked_until"] or "") < now)), key=lambda r: r["received_at"])[: p["p_limit"]]
        for r in rows:
            r.update(status="processing", attempts=r["attempts"] + 1, locked_until=self._lease(p["p_lease_seconds"]))
        return [dict(r) for r in rows]

    def rpc_tenant_stats(self, p):
        """Mirrors migrations/0004 (returns the jsonb object directly, as PostgREST does for a scalar jsonb function)."""
        tid = p["p_tenant_id"]
        mine = lambda t: [r for r in self.tables.get(t, []) if r.get("tenant_id") == tid]
        by = lambda t: {k: sum(1 for r in mine(t) if r["status"] == k) for k in {r["status"] for r in mine(t)}}
        msgs = mine("messages")
        paid = [r for r in mine("orders") if r["status"] in ("PAID", "PREPARING", "READY", "COMPLETED")]
        return {"conversations": by("conversations"), "orders": by("orders"), "bookings": by("bookings"),
                "inbound_24h": sum(1 for m in msgs if m["direction"] == "inbound" and m["created_at"] >= p["p_since24"]),
                "bot_24h": sum(1 for m in msgs if m["direction"] == "outbound" and m.get("sender_type") == "bot" and m["created_at"] >= p["p_since24"]),
                "revenue_total": sum(r["subtotal_minor"] for r in paid),
                "revenue_7d": sum(r["subtotal_minor"] for r in paid if (r.get("paid_at") or "") >= p["p_since7d"])}

    def rpc_platform_conversation_counts(self, p):
        out = {}
        for r in self.tables.get("conversations", []):
            d = out.setdefault(r["tenant_id"], {"tenant_id": r["tenant_id"], "total": 0, "handoff": 0})
            d["total"] += 1
            d["handoff"] += r["status"] == "handoff"
        return list(out.values())

    def rpc_activate_config_tx(self, p):
        """Mirrors migrations/0002: one transaction => either (old archived AND new active) or nothing changed."""
        rows, tid = self.tables.setdefault("tenant_configs", []), p["p_tenant_id"]
        if any(r["tenant_id"] == tid and r["version"] == p["p_version"] for r in rows):
            raise Exception("duplicate key value violates unique constraint tenant_configs_tenant_id_version_key (23505)")
        for r in rows:
            if r["tenant_id"] == tid and r["status"] == "active": r["status"] = "archived"
        row = {"id": str(uuid.uuid4()), "tenant_id": tid, "version": p["p_version"], "status": "active", "config": p["p_config"], "compiled": p["p_compiled"],
               "validation_report": p["p_report"] or {}, "created_by": p["p_created_by"], "created_at": self.stamp(), "activated_at": self.stamp()}
        rows.append(row)
        return [dict(row)]

    def rpc_confirm_booking_tx(self, p):
        b = next((b for b in self.tables.get("bookings", []) if b["id"] == p["p_booking_id"] and b["tenant_id"] == p["p_tenant_id"] and b["status"] == "pending"), None)
        if not b: return []
        b.update(status=p["p_status"], confirmed_at=p["p_confirmed_at"] if p["p_status"] == "confirmed" else None, staff_notes=p["p_notes"])
        self.tables.setdefault("messages", []).append({"id": str(uuid.uuid4()), "tenant_id": b["tenant_id"], "conversation_id": b["conversation_id"], "sender_type": "system",
                                                       "direction": "outbound", "content": f"Booking {p['p_status']}: {b['subject']}", "created_at": self.stamp()})
        return [dict(b)]


def cosine(a, b) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def bow_embed(text: str, dims: int = 64) -> list[float]:
    v = [0.0] * dims
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[zlib.crc32(w.encode()) % dims] += 1.0
    return v


class FakeLLM:
    """Scriptable. `script` items are Interpretation/dict/callable(system, prompt); an exhausted script returns a bland reply."""
    def __init__(self):
        self.script: list = []
        self.calls: list[dict] = []
        self.unavailable = False
        self.summaries: list[str] = []
        self.embed_fail = False

    def say(self, reply: str, *actions: tuple, intent: str | None = None) -> "FakeLLM":
        self.script.append(Interpretation(reply=reply, intent=intent, actions=[Action(function=f, args=a) for f, a in actions]))
        return self

    async def interpret(self, tenant_id, *, system, prompt) -> InterpretResult:
        self.calls.append({"tenant_id": tenant_id, "system": system, "prompt": prompt})
        if self.unavailable: raise LLMUnavailable("down")
        item = self.script.pop(0) if self.script else Interpretation(reply="Okay! What else can I help with?")
        if callable(item): item = item(system, prompt)
        return InterpretResult(item, "fake")

    async def summarize(self, tenant_id, transcript) -> str:
        self.summaries.append(transcript)
        return f"SUMMARY#{len(self.summaries)}"

    async def embed(self, tenant_id, text, **_) -> list[float]:
        if self.embed_fail: raise LLMUnavailable("no embeddings")
        return bow_embed(text)


class FakeAdapter:
    kind = "whatsapp_evolution"
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.media: list[tuple[str, str, str]] = []
        self.fail_sends = False
        self.attempts = 0
        self.audio: bytes | None = b"fake-audio"
    def verify(self, **_): return True
    def parse(self, body): return None
    async def send_text(self, to, text, *, typing_delay=0.0):
        self.attempts += 1
        if self.fail_sends: raise RuntimeError("network down")
        self.sent.append((to, text))
        self.typing_delays = getattr(self, "typing_delays", []) + [typing_delay]
    async def send_media(self, to, url, media_type, caption=""): self.media.append((to, url, media_type))
    async def download_media(self, raw): return self.audio
    async def connection_status(self): return "CONNECTED"
    async def fetch_qr(self): return {"connected": False, "qr_base64": "data:image/png;base64,AAAA", "pairing_code": "ABCD-1234"}
    async def set_webhook(self, url, headers):
        self.webhooks = getattr(self, "webhooks", []) + [(url, dict(headers))]
    def texts(self, to: str | None = None) -> list[str]: return [t for (n, t) in self.sent if to is None or n == to]


class FakeAdapterFactory:
    def __init__(self, adapter): self.adapter = adapter
    def for_channel(self, channel): return self.adapter


class FakePayments:
    name = "paystack"
    def __init__(self):
        self.initialized: list[dict] = []
        self.verify_result = {"status": "success", "amount_minor": None}   # None => echo the initialized amount
        self.fail_init = False
    async def initialize(self, *, reference, amount_minor, currency, phone, metadata):
        if self.fail_init: raise RuntimeError("paystack down")
        self.initialized.append({"reference": reference, "amount_minor": amount_minor})
        return {"authorization_url": f"https://pay.test/{reference}", "reference": reference}
    async def verify(self, reference):
        amt = self.verify_result["amount_minor"]
        if amt is None: amt = next((i["amount_minor"] for i in self.initialized if i["reference"] == reference), 0)
        return {"status": self.verify_result["status"], "amount_minor": amt}
    def verify_signature(self, headers, body): return {k.lower(): v for k, v in headers.items()}.get("x-test-sig") == "valid"
    def extract_reference(self, event): return (event.get("data") or {}).get("reference") if event.get("event") == "charge.success" else None


def inbound(text: str, phone: str = "2348011111111", msg_id: str | None = None, kind: str = "text", name: str | None = "Ada") -> InboundMessage:
    return InboundMessage(provider_message_id=msg_id or f"m-{uuid.uuid4().hex[:10]}", phone=phone, text=text, kind=kind, push_name=name, raw={"key": {"id": msg_id}})

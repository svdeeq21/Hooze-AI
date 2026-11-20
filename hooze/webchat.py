"""Try a client's assistant in a browser, with REAL AI replies and no WhatsApp.

Two ways in, one engine:
  * the TEST CHAT in the dashboard (operator or the client's own key), to check a setup before and after going live;
  * DEMO LINKS: a private link for a prospect (expires, capped number of messages) that opens a chat page with no login.

Each browser session is a separate test customer (a made-up phone number starting 000, which no real WhatsApp number does) on the
client's own 'web_demo' channel. The turn runs through the exact same pipeline as WhatsApp: same knowledge, menu, rules, hand-offs.
Differences, all deliberate:
  * nothing is sent anywhere: replies come back to the browser and owner alerts are shown there instead of texting the team;
  * payments are SIMULATED (a "Pay now" button confirms them), never a real Paystack charge;
  * the chat always behaves as Full auto, even for a client in After-hours or Assist mode, so you see what the assistant would say.
Test customers live in the client's database like any other (they show in Conversations / Orders / Bookings) until cleared.
"""
from __future__ import annotations
import hashlib, re, uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from hooze.channels.base import InboundMessage
from hooze.channels.webdemo import WebDemoAdapter
from hooze.kernel import capture
from hooze.kernel.db import first
from hooze.security import new_secret, sha256_hex

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
TEST_PHONE_PREFIX = "000"
DEMO_PAY_HOST = "https://pay.hooze.demo"


class WebChatError(Exception):
    """Safe to show to the person in the chat."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def session_phone(tenant_id: str, session: str) -> str:
    if not SESSION_RE.match(session or ""):
        raise WebChatError(422, "Invalid chat session.")
    h = int(hashlib.sha256(f"{tenant_id}:{session}".encode()).hexdigest()[:16], 16)
    return TEST_PHONE_PREFIX + str(h % 10**10).zfill(10)


async def ensure_web_channel(pdb, tenant_id: str) -> dict:
    """The client's web channel (created once). Status stays 'pending' so WhatsApp sending, monitoring and alerting never use it."""
    row = first(await pdb.table("channels").select("*").eq("tenant_id", tenant_id).eq("kind", "web_demo").limit(1).execute())
    if row:
        return row
    try:
        return first(await pdb.table("channels").insert({"tenant_id": tenant_id, "kind": "web_demo", "instance_name": f"web-{tenant_id}",
                                                          "config": {}, "secret_hash": sha256_hex(new_secret()), "status": "pending"}).execute())
    except Exception:
        row = first(await pdb.table("channels").select("*").eq("tenant_id", tenant_id).eq("kind", "web_demo").limit(1).execute())
        if row:
            return row                                   # created by a concurrent request
        raise


class DemoPayments:
    """Checkout in a test chat produces a pretend payment link; the chat's "Pay now" button confirms it."""
    name = "demo"

    async def initialize(self, *, reference, amount_minor, currency, phone, metadata) -> dict:
        return {"authorization_url": f"{DEMO_PAY_HOST}/{reference}", "reference": reference}

    async def verify(self, reference: str) -> dict:
        raise RuntimeError("test-chat payments are confirmed from the chat, not by a provider")

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        return False

    def extract_reference(self, event: dict) -> Optional[str]:
        return None


class _WebFactory:
    """Adapters for a test-chat turn: the web channel never touches WhatsApp; anything else defers to the real factory."""
    def __init__(self, inner):
        self.inner = inner

    def for_channel(self, channel: dict):
        return WebDemoAdapter() if channel.get("kind") == "web_demo" else self.inner.for_channel(channel)


class _FullAutoConfigs:
    """Test chats always run in Full auto, whatever mode the client uses with real customers."""
    def __init__(self, inner):
        self.inner = inner

    def invalidate(self, tenant_id: str) -> None:
        self.inner.invalidate(tenant_id)

    async def load_active(self, tenant_id: str):
        cfg = await self.inner.load_active(tenant_id)
        return cfg.model_copy(update={"mode": "full_auto"}) if cfg is not None else None


async def _no_sleep(_s: float) -> None:
    return None


def _event_view(e: dict) -> dict:
    keep = ("tools", "from_state", "to_state", "intent", "reason", "provider", "timings", "tool", "error", "cap")
    md = e.get("metadata") or {}
    return {"event": e["event"], "severity": e["severity"], **{k: md[k] for k in keep if k in md}}


async def _state(pdb, tenant_id: str, phone: str) -> dict:
    """What the assistant has captured about this test customer so far (the "behind the scenes" panel)."""
    db = pdb.for_tenant(tenant_id)
    contact = first(await db.table("contacts").select("*").eq("phone", phone).limit(1).execute())
    if not contact:
        return {"slots": {}, "stage": None, "status": None, "order": None, "booking": None, "payment_due": False}
    conv = first(await db.table("conversations").select("*").eq("contact_id", contact["id"]).limit(1).execute())
    orders = (await db.table("orders").select("*").eq("contact_id", contact["id"]).in_("status", ["CART", "PAYMENT_PENDING", "PAID", "PREPARING", "READY"])
              .order("created_at", desc=True).limit(1).execute()).data or []
    order = None
    if orders:
        o = orders[0]
        items = (await db.table("order_items").select("product_name, quantity, line_total_minor, modifiers").eq("order_id", o["id"]).order("created_at").execute()).data or []
        order = {"id": o["id"], "status": o["status"], "subtotal_minor": o.get("subtotal_minor", 0), "fulfillment": o.get("fulfillment_method"),
                 "address": o.get("delivery_address"),
                 "items": [{"name": i["product_name"], "quantity": i["quantity"], "line_total_minor": i["line_total_minor"],
                            "options": [m.get("name") for m in (i.get("modifiers") or []) if isinstance(m, dict)]} for i in items]}
    b = first(await db.table("bookings").select("*").eq("contact_id", contact["id"]).order("created_at", desc=True).limit(1).execute())
    return {"slots": (contact.get("attributes") or {}).get("slots") or {}, "stage": (conv or {}).get("state"),
            "status": "handoff" if contact.get("ai_paused") or (conv or {}).get("status") == "handoff" else "active",
            "order": order, "booking": {"subject": b["subject"], "when": b.get("preferred_text"), "status": b["status"]} if b else None,
            "payment_due": bool(order and order["status"] == "PAYMENT_PENDING")}


def _result(box: dict, phone: str, outcome_status: str, state: dict) -> dict:
    replies = [{"type": s["type"], "text": s.get("text", ""), **({"url": s["url"]} if s.get("url") else {})} for s in box["sent"] if s.get("to") == phone]
    return {"status": outcome_status, "replies": replies, "owner_alerts": list(box["staff"]),
            "activity": [_event_view(e) for e in box["events"]], "state": state}


async def run_turn(c, tenant_id: str, session: str, text: str, name: Optional[str] = None, *, max_chars: int = 1000) -> dict:
    """One customer message through the real pipeline. Returns what the customer would see and what happened behind the scenes."""
    from hooze.runtime.pipeline import Pipeline       # lazy: pipeline imports capabilities, which import this package's siblings
    text = (text or "").strip()
    if not text:
        raise WebChatError(422, "Type a message first.")
    phone = session_phone(tenant_id, session)
    tenant = first(await c.pdb.table("tenants").select("*").eq("id", tenant_id).limit(1).execute())
    if not tenant:
        raise WebChatError(404, "Client not found.")
    if tenant["status"] != "active" or await c.configs.load_active(tenant_id) is None:
        raise WebChatError(409, "This assistant isn't live yet. Finish Setup and press Go live, then try again.")
    channel = await ensure_web_channel(c.pdb, tenant_id)

    async def demo_payments():
        return DemoPayments()
    pipe = Pipeline(platform_db=c.pdb, llm=c.llm, events=c.events, adapters=_WebFactory(c.adapters), configs=_FullAutoConfigs(c.configs),
                    settings=c.settings, clock=c.clock, sleep=_no_sleep, transcriber=None, extras_for=lambda _tid: {"payments": demo_payments})
    inbound = InboundMessage(provider_message_id=f"web-{uuid.uuid4().hex}", phone=phone, text=text[:max_chars], kind="text",
                             push_name=(name or "Test customer")[:60])
    with capture.capturing() as box:
        box["channel_id"] = channel["id"]
        outcome = await pipe.handle_inbound(channel, inbound)
    return _result(box, phone, outcome.status, await _state(c.pdb, tenant_id, phone))


async def simulate_payment(c, tenant_id: str, session: str) -> dict:
    """The test customer "pays" the pending checkout: the same confirmation path as a verified Paystack payment."""
    from hooze.runtime.tenant_runtime import build_tenant_runtime
    phone = session_phone(tenant_id, session)
    db = c.pdb.for_tenant(tenant_id)
    contact = first(await db.table("contacts").select("*").eq("phone", phone).limit(1).execute())
    order = contact and first(await db.table("orders").select("*").eq("contact_id", contact["id"]).eq("status", "PAYMENT_PENDING").limit(1).execute())
    pay = order and first(await db.table("payments").select("*").eq("order_id", order["id"]).eq("status", "pending").order("created_at", desc=True).limit(1).execute())
    if not pay or pay.get("provider") != "demo":
        raise WebChatError(409, "There's no test payment waiting. Ask the assistant to check out first.")
    cfg = await c.configs.load_active(tenant_id)
    if cfg is None or "ordering" not in cfg.capabilities:
        raise WebChatError(409, "This assistant doesn't take orders.")
    channel = await ensure_web_channel(c.pdb, tenant_id)
    with capture.capturing() as box:
        box["channel_id"] = channel["id"]
        _, caps = build_tenant_runtime(pdb=c.pdb, adapters=_WebFactory(c.adapters), events=c.events, llm=c.llm, cfg=cfg, tenant_id=tenant_id, clock=c.clock)
        await caps["ordering"].confirm_payment(pay["reference"], {"status": "success", "amount_minor": pay["amount_minor"], "currency": pay.get("currency")})
    return _result(box, phone, "paid", await _state(c.pdb, tenant_id, phone))


async def clear_test_chats(pdb, tenant_id: str) -> dict:
    """Delete every test customer on this client's web channel, with their chats, orders and bookings. Real customers are untouched."""
    ch = first(await pdb.table("channels").select("id").eq("tenant_id", tenant_id).eq("kind", "web_demo").limit(1).execute())
    if not ch:
        return {"customers": 0}
    db = pdb.for_tenant(tenant_id)
    convs = (await db.table("conversations").select("id, contact_id").eq("channel_id", ch["id"]).execute()).data or []
    contact_ids = sorted({cv["contact_id"] for cv in convs})
    for cid in contact_ids:
        contact = first(await db.table("contacts").select("phone").eq("id", cid).limit(1).execute())
        if not contact or not str(contact["phone"]).startswith(TEST_PHONE_PREFIX):
            continue                                       # belt and braces: only ever delete made-up test numbers
        for o in (await db.table("orders").select("id").eq("contact_id", cid).execute()).data or []:
            await db.table("payments").delete().eq("order_id", o["id"]).execute()
            await db.table("order_items").delete().eq("order_id", o["id"]).execute()
            await db.table("orders").delete().eq("id", o["id"]).execute()
        await db.table("bookings").delete().eq("contact_id", cid).execute()
        for cv in [x for x in convs if x["contact_id"] == cid]:
            await db.table("reply_drafts").delete().eq("conversation_id", cv["id"]).execute()
            await db.table("nudge_state").delete().eq("conversation_id", cv["id"]).execute()
            await db.table("messages").delete().eq("conversation_id", cv["id"]).execute()
            await db.table("conversations").delete().eq("id", cv["id"]).execute()
        await db.table("contacts").delete().eq("id", cid).execute()
    return {"customers": len(contact_ids)}


# ── demo links ───────────────────────────────────────────────────────────
def _now(c) -> datetime:
    return c.clock()


def _parse_ts(v: Any) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def create_demo_link(c, tenant_id: str, label: str, days: int, max_messages: int) -> dict:
    await ensure_web_channel(c.pdb, tenant_id)
    token = new_secret(24)
    row = first(await c.pdb.table("demo_links").insert({"tenant_id": tenant_id, "token_hash": sha256_hex(token), "label": label[:80],
                                                          "max_messages": max_messages, "used_messages": 0,
                                                          "expires_at": (_now(c) + timedelta(days=days)).isoformat()}).execute())
    return {**link_view(row), "token": token}


def link_view(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "label", "max_messages", "used_messages", "expires_at", "revoked_at", "created_at")}


async def resolve_demo_link(c, token: str) -> dict:
    if not token or len(token) > 100:
        raise WebChatError(404, "This demo link doesn't exist.")
    row = first(await c.pdb.table("demo_links").select("*").eq("token_hash", sha256_hex(token)).limit(1).execute())
    if not row or row.get("revoked_at"):
        raise WebChatError(404, "This demo link doesn't exist or was switched off.")
    if _parse_ts(row["expires_at"]) <= _now(c):
        raise WebChatError(410, "This demo link has expired. Ask for a new one.")
    return row


async def spend_demo_message(c, row: dict) -> dict:
    """Count one message against the link's allowance. Conditional update, so two tabs can't both take the last message."""
    for _ in range(4):
        used = int(row["used_messages"])
        if used >= int(row["max_messages"]):
            raise WebChatError(429, "This demo has used all its messages. Ask for a new link to keep chatting.")
        won = (await c.pdb.table("demo_links").update({"used_messages": used + 1}).eq("id", row["id"]).eq("used_messages", used).execute()).data
        if won:
            return won[0]
        row = first(await c.pdb.table("demo_links").select("*").eq("id", row["id"]).limit(1).execute()) or row
    raise WebChatError(429, "The demo is busy. Try again in a moment.")


async def demo_intro(c, row: dict) -> dict:
    """What the public demo page shows before the first message: who it is, their welcome, and a few things to try."""
    tid = row["tenant_id"]
    tenant = first(await c.pdb.table("tenants").select("name, status").eq("id", tid).limit(1).execute()) or {}
    cfg_row = first(await c.pdb.for_tenant(tid).table("tenant_configs").select("config").eq("status", "active").limit(1).execute())
    bc = (cfg_row or {}).get("config") or {}
    goal = (bc.get("sales_process") or {}).get("goal")
    offers = [o.get("name") for o in bc.get("offerings") or [] if o.get("name")]
    faqs = [f.get("question") for f in bc.get("faqs") or [] if f.get("question")]
    ideas = []
    if goal == "order":
        ideas += ["What's on the menu?"] + ([f"I'd like 2 {offers[0]}"] if offers else [])
    elif offers:
        ideas += [f"Tell me about {offers[0]}", "How much does it cost?"]
    ideas += faqs[:2] + ["Can I talk to a real person?"]
    identity = bc.get("identity") or {}
    return {"business": tenant.get("name") or identity.get("name") or "", "description": identity.get("description") or "",
            "goal": goal, "live": tenant.get("status") == "active" and bool(cfg_row), "suggestions": ideas[:5],
            "messages_left": max(0, int(row["max_messages"]) - int(row["used_messages"])), "expires_at": row["expires_at"]}

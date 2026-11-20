"""Routes for: the dashboard TEST CHAT, ASSIST-mode drafts, DEMO LINKS (operator side) and the PUBLIC demo chat a prospect opens.

The public routes (/v1/demo/...) need no API key: the link's secret token is the key. Each link expires, has a message allowance,
and each browser session is capped, so a leaked link can cost at most `max_messages` AI replies.
"""
from __future__ import annotations
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from hooze import webchat
from hooze.api.deps import get_container, require_tenant, require_tenant_admin
from hooze.kernel.db import first
from hooze.runtime.drafts import DraftError, discard_draft, send_draft
from hooze.security import Principal

router = APIRouter()
PUBLIC_SESSION_CAP = 30            # messages one prospect's browser can send on one demo link
PUBLIC_MAX_CHARS = 500


def _raise(e: Exception):
    if isinstance(e, (webchat.WebChatError, DraftError)):
        raise HTTPException(e.status, str(e))
    raise e


class ChatIn(BaseModel):
    session: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    name: Optional[str] = Field(None, max_length=60)


class SessionIn(BaseModel):
    session: str = Field(min_length=8, max_length=64)


async def _history(pdb, tenant_id: str, session: str) -> list[dict]:
    phone = webchat.session_phone(tenant_id, session)
    db = pdb.for_tenant(tenant_id)
    contact = first(await db.table("contacts").select("id").eq("phone", phone).limit(1).execute())
    conv = contact and first(await db.table("conversations").select("id").eq("contact_id", contact["id"]).limit(1).execute())
    if not conv:
        return []
    rows = (await db.table("messages").select("direction, sender_type, content, content_type, created_at, meta").eq("conversation_id", conv["id"])
            .order("created_at").limit(300).execute()).data or []
    return [{"from": "customer" if r["direction"] == "inbound" else ("team" if r["sender_type"] == "human" else "assistant"),
             "text": r["content"], "url": (r.get("meta") or {}).get("media_url"), "at": r["created_at"]}
            for r in rows if r["sender_type"] != "system"]


# ═══════════════════════ dashboard test chat ═══════════════════════
@router.post("/v1/tenants/{tenant_id}/test-chat", dependencies=[Depends(require_tenant)])
async def test_chat(tenant_id: str, body: ChatIn, request: Request):
    """Send one message as a test customer and get the assistant's real reply (nothing is sent on WhatsApp)."""
    try:
        return await webchat.run_turn(get_container(request), tenant_id, body.session, body.text, body.name)
    except Exception as e:
        _raise(e)


@router.get("/v1/tenants/{tenant_id}/test-chat", dependencies=[Depends(require_tenant)])
async def test_chat_history(tenant_id: str, session: str, request: Request):
    c = get_container(request)
    try:
        return {"messages": await _history(c.pdb, tenant_id, session), "state": await webchat._state(c.pdb, tenant_id, webchat.session_phone(tenant_id, session))}
    except Exception as e:
        _raise(e)


@router.post("/v1/tenants/{tenant_id}/test-chat/pay", dependencies=[Depends(require_tenant)])
async def test_chat_pay(tenant_id: str, body: SessionIn, request: Request):
    try:
        return await webchat.simulate_payment(get_container(request), tenant_id, body.session)
    except Exception as e:
        _raise(e)


@router.delete("/v1/tenants/{tenant_id}/test-chat", dependencies=[Depends(require_tenant_admin)])
async def test_chat_clear(tenant_id: str, request: Request):
    """Remove every test customer (and their chats, orders, bookings) from this client. Real customers are never touched."""
    c = get_container(request)
    r = await webchat.clear_test_chats(c.pdb, tenant_id)
    await c.events.info("TEST_CHATS_CLEARED", tenant_id=tenant_id, customers=r["customers"])
    return r


@router.post("/v1/tenants/{tenant_id}/test-chat/enable", dependencies=[Depends(require_tenant_admin)])
async def test_chat_enable(tenant_id: str, request: Request):
    """Give this client its web test channel, so it can go live and be demoed before a WhatsApp number is paired."""
    c = get_container(request)
    if not first(await c.pdb.table("tenants").select("id").eq("id", tenant_id).limit(1).execute()):
        raise HTTPException(404, "tenant not found")
    ch = await webchat.ensure_web_channel(c.pdb, tenant_id)
    return {"enabled": True, "channel_id": ch["id"]}


# ═══════════════════════ assist mode: drafts ═══════════════════════
@router.get("/v1/tenants/{tenant_id}/drafts", dependencies=[Depends(require_tenant)])
async def list_drafts(tenant_id: str, request: Request, conversation_id: Optional[str] = None):
    db = get_container(request).pdb.for_tenant(tenant_id)
    q = db.table("reply_drafts").select("*").eq("status", "pending")
    if conversation_id:
        q = q.eq("conversation_id", conversation_id)
    rows = (await q.order("created_at").limit(200).execute()).data or []
    keep = ("id", "conversation_id", "contact_id", "kind", "content", "media_url", "media_type", "created_at")
    return {"drafts": [{k: r.get(k) for k in keep} for r in rows]}


class DraftSendIn(BaseModel):
    text: Optional[str] = Field(None, max_length=4000)


@router.post("/v1/tenants/{tenant_id}/drafts/{draft_id}/send")
async def approve_draft(tenant_id: str, draft_id: str, body: DraftSendIn, request: Request, p: Principal = Depends(require_tenant)):
    c = get_container(request)
    actor = "platform" if p.kind == "platform" else f"key:{p.key_id}"
    try:
        r = await send_draft(pdb=c.pdb, adapters=c.adapters, tenant_id=tenant_id, draft_id=draft_id, clock=c.clock, actor=actor, text=body.text)
    except Exception as e:
        _raise(e)
    await c.events.info("DRAFT_SENT", tenant_id=tenant_id, ref_type="reply_draft", ref_id=draft_id, edited=r["edited"])
    return r


@router.post("/v1/tenants/{tenant_id}/drafts/{draft_id}/discard", dependencies=[Depends(require_tenant)])
async def drop_draft(tenant_id: str, draft_id: str, request: Request):
    c = get_container(request)
    if not await discard_draft(pdb=c.pdb, tenant_id=tenant_id, draft_id=draft_id, clock=c.clock, actor="dashboard"):
        raise HTTPException(409, "That draft was already sent or discarded.")
    return {"discarded": True}


# ═══════════════════════ weekly owner report ═══════════════════════
async def _report_parts(c, tenant_id: str):
    tenant = first(await c.pdb.table("tenants").select("*").eq("id", tenant_id).limit(1).execute())
    cfg = tenant and await c.configs.load_active(tenant_id)
    if not cfg:
        raise HTTPException(409, "This client isn't live yet, so there is nothing to report.")
    return tenant, cfg


@router.get("/v1/tenants/{tenant_id}/reports/weekly", dependencies=[Depends(require_tenant)])
async def weekly_preview(tenant_id: str, request: Request):
    """Exactly what the owner would receive on Monday morning, for the last 7 days. Sends nothing."""
    from hooze.reports import weekly_numbers, weekly_text
    c = get_container(request)
    tenant, cfg = await _report_parts(c, tenant_id)
    n = await weekly_numbers(c.pdb, tenant_id, cfg, c.clock())
    return {"text": weekly_text(tenant["name"], cfg, n, c.clock()), "numbers": n, "staff_numbers": len(cfg.staff_numbers),
            "automatic": bool(getattr(c.settings, "weekly_report_enabled", False))}


@router.post("/v1/tenants/{tenant_id}/reports/weekly/send", dependencies=[Depends(require_tenant_admin)])
async def weekly_send_now(tenant_id: str, request: Request):
    """Send the report to the client's staff numbers now (the Monday report still goes out as usual)."""
    from hooze.reports import weekly_numbers, weekly_text
    from hooze.runtime.tenant_runtime import build_tenant_runtime
    c = get_container(request)
    tenant, cfg = await _report_parts(c, tenant_id)
    ctx, _ = build_tenant_runtime(pdb=c.pdb, adapters=c.adapters, events=c.events, llm=c.llm, cfg=cfg, tenant_id=tenant_id, clock=c.clock)
    delivered = await ctx.outbox.notify_staff(weekly_text(tenant["name"], cfg, await weekly_numbers(c.pdb, tenant_id, cfg, c.clock()), c.clock()))
    await c.events.info("WEEKLY_REPORT_SENT_MANUALLY", tenant_id=tenant_id, delivered=delivered)
    return {"delivered": delivered}


# ═══════════════════════ demo links (operator side) ═══════════════════════
class LinkIn(BaseModel):
    label: str = Field("", max_length=80)
    days: int = Field(7, ge=1, le=90)
    max_messages: int = Field(100, ge=10, le=2000)


@router.get("/v1/tenants/{tenant_id}/demo-links", dependencies=[Depends(require_tenant_admin)])
async def list_links(tenant_id: str, request: Request):
    rows = (await get_container(request).pdb.table("demo_links").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).limit(100).execute()).data or []
    return {"links": [webchat.link_view(r) for r in rows]}


@router.post("/v1/tenants/{tenant_id}/demo-links", dependencies=[Depends(require_tenant_admin)])
async def create_link(tenant_id: str, body: LinkIn, request: Request):
    """A private chat link for a prospect. The token is shown ONCE; only its hash is stored."""
    c = get_container(request)
    if not first(await c.pdb.table("tenants").select("id").eq("id", tenant_id).limit(1).execute()):
        raise HTTPException(404, "tenant not found")
    out = await webchat.create_demo_link(c, tenant_id, body.label, body.days, body.max_messages)
    await c.events.info("DEMO_LINK_CREATED", tenant_id=tenant_id, ref_type="demo_link", ref_id=out["id"], days=body.days, max_messages=body.max_messages)
    return out


@router.delete("/v1/tenants/{tenant_id}/demo-links/{link_id}", dependencies=[Depends(require_tenant_admin)])
async def revoke_link(tenant_id: str, link_id: str, request: Request):
    c = get_container(request)
    rows = (await c.pdb.table("demo_links").update({"revoked_at": c.clock().isoformat()}).eq("id", link_id).eq("tenant_id", tenant_id).execute()).data
    if not rows:
        raise HTTPException(404, "link not found")
    return {"revoked": True}


# ═══════════════════════ public demo chat (no API key; the link token is the key) ═══════════════════════
@router.get("/v1/demo/{token}")
async def demo_info(token: str, request: Request):
    c = get_container(request)
    try:
        return await webchat.demo_intro(c, await webchat.resolve_demo_link(c, token))
    except Exception as e:
        _raise(e)


@router.get("/v1/demo/{token}/history")
async def demo_history(token: str, session: str, request: Request):
    c = get_container(request)
    try:
        row = await webchat.resolve_demo_link(c, token)
        tid = row["tenant_id"]
        return {"messages": await _history(c.pdb, tid, session), "state": await webchat._state(c.pdb, tid, webchat.session_phone(tid, session))}
    except Exception as e:
        _raise(e)


@router.post("/v1/demo/{token}/messages")
async def demo_message(token: str, body: ChatIn, request: Request):
    c = get_container(request)
    try:
        row = await webchat.resolve_demo_link(c, token)
        tid = row["tenant_id"]
        phone = webchat.session_phone(tid, body.session)
        db = c.pdb.for_tenant(tid)
        contact = first(await db.table("contacts").select("id").eq("phone", phone).limit(1).execute())
        conv = contact and first(await db.table("conversations").select("user_message_count").eq("contact_id", contact["id"]).limit(1).execute())
        if conv and int(conv.get("user_message_count") or 0) >= PUBLIC_SESSION_CAP:
            raise webchat.WebChatError(429, "That's the end of this demo conversation. Get in touch to see Hooze on your own business.")
        row = await webchat.spend_demo_message(c, row)
        out = await webchat.run_turn(c, tid, body.session, body.text, body.name or "Demo visitor", max_chars=PUBLIC_MAX_CHARS)
        out["messages_left"] = max(0, int(row["max_messages"]) - int(row["used_messages"]))
        return out
    except Exception as e:
        _raise(e)


@router.post("/v1/demo/{token}/pay")
async def demo_pay(token: str, body: SessionIn, request: Request):
    c = get_container(request)
    try:
        row = await webchat.resolve_demo_link(c, token)
        return await webchat.simulate_payment(c, row["tenant_id"], body.session)
    except Exception as e:
        _raise(e)

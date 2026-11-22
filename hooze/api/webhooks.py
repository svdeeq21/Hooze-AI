"""Inbound webhooks. Both FAIL CLOSED: unauthenticated == rejected, never processed."""
from __future__ import annotations
import json, uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request

from hooze.api.deps import get_container
from hooze.kernel.db import first
from hooze.runtime.tenant_runtime import build_tenant_runtime

router = APIRouter()


PAYMENT_WEBHOOK_MAX_BYTES = 1_000_000          # Paystack events are a few KB


def _uuid_or_404(v: str) -> str:
    try:
        return str(uuid.UUID(v))
    except ValueError:
        raise HTTPException(404, "not found")


async def read_limited(request: Request, limit: int) -> bytes:
    """The raw body, refusing (413) anything over `limit` WITHOUT buffering it all first. These routes are reachable by anyone on the
    internet; an unbounded body (a multi-GB upload to a valid webhook URL) could otherwise exhaust the server's memory."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "payload too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/webhooks/whatsapp/{channel_id}")
async def whatsapp_webhook(channel_id: str, request: Request, background: BackgroundTasks, token: Optional[str] = Query(None)):
    c = get_container(request)
    channel = first(await c.pdb.table("channels").select("*").eq("id", _uuid_or_404(channel_id)).limit(1).execute())
    if not channel or channel["status"] != "active":
        raise HTTPException(404, "not found")
    raw = await read_limited(request, int(getattr(c.settings, "webhook_max_bytes", 5_000_000) or 5_000_000))
    try:
        body = json.loads(raw)
    except Exception:
        raise HTTPException(400, "invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid JSON")             # a list/string body used to crash with a 500 inside verify()
    adapter = c.adapters.for_channel(channel)
    provided = request.headers.get("x-hooze-secret")
    if not provided and token:
        if getattr(c.settings, "webhook_allow_query_token", False):
            provided = token
        else:
            # The secret in a URL lands in proxy/access logs. Say exactly why this was refused, so a hand-configured number is easy to fix.
            await c.events.warn("WEBHOOK_QUERY_TOKEN_REJECTED", tenant_id=channel["tenant_id"], channel_id=channel_id,
                                hint="send the secret in the x-hooze-secret header (re-provision/rotate the webhook from the dashboard)")
    if not adapter.verify(provided_secret=provided, body=body, channel=channel):
        await c.events.warn("WEBHOOK_AUTH_FAILED", tenant_id=channel["tenant_id"], channel_id=channel_id, ip=request.client.host if request.client else None)
        raise HTTPException(401, "unauthorized")
    inbound = adapter.parse(body)
    if inbound is None or inbound.from_me or inbound.is_group or not inbound.phone:
        return {"status": "ignored"}                                     # nothing to answer: don't store noise
    if c.inbox is None:                                                  # legacy path (no durable queue configured)
        background.add_task(c.pipeline.handle_inbound, channel, inbound)
        return {"status": "accepted"}
    try:
        event_id, is_new = await c.inbox.enqueue(channel, body, inbound.provider_message_id, phone="".join(ch for ch in inbound.phone if ch.isdigit()))
    except Exception as e:
        # We could NOT save the message. Say so (5xx) rather than acknowledge something we would lose; the alert rules pick this up.
        await c.events.error("INBOUND_ENQUEUE_FAILED", tenant_id=channel["tenant_id"], channel_id=channel_id, error=str(e)[:200])
        raise HTTPException(503, "temporarily unavailable")
    if not is_new:
        return {"status": "duplicate"}
    background.add_task(c.inbox.process, event_id)                       # ack fast (the row is already saved); provider must not wait on LLM latency
    return {"status": "accepted"}


@router.post("/v1/webhooks/payments/{tenant_id}/{provider}")
async def payment_webhook(tenant_id: str, provider: str, request: Request):
    """Processed INLINE (not in the background) on purpose: if confirmation fails we return 500 and the provider
    retries. The conditional-UPDATE guard in ordering.confirm_payment makes retries exactly-once safe."""
    c = get_container(request)
    tenant_id = _uuid_or_404(tenant_id)
    raw = await read_limited(request, PAYMENT_WEBHOOK_MAX_BYTES)
    try:
        prov = await c.payments_for(tenant_id)
    except LookupError:
        raise HTTPException(404, "not found")
    if prov.name != provider or not prov.verify_signature(dict(request.headers), raw):
        await c.events.warn("PAYMENT_WEBHOOK_AUTH_FAILED", tenant_id=tenant_id)
        raise HTTPException(401, "unauthorized")
    try:
        ref = prov.extract_reference(json.loads(raw))
    except Exception:
        raise HTTPException(400, "invalid JSON")
    if not ref:
        return {"status": "ignored"}
    try:
        verified = await prov.verify(ref)                    # never trust the webhook body alone (status AND amount are re-checked)
        cfg = await c.configs.load_active(tenant_id)
        _, caps = build_tenant_runtime(pdb=c.pdb, adapters=c.adapters, events=c.events, llm=c.llm, cfg=cfg, tenant_id=tenant_id, clock=c.clock, extras=c.extras_for(tenant_id))
        order = await caps["ordering"].confirm_payment(ref, verified)
    except Exception as e:
        await c.events.error("PAYMENT_CONFIRM_FAILED", tenant_id=tenant_id, reference=ref, error=str(e)[:200])
        raise HTTPException(500, "confirmation failed")     # 5xx => provider retries
    return {"status": "confirmed" if order else "noop"}

"""Platform + tenant admin API (the backend of the future Hooze dashboard / Control Center)."""
from __future__ import annotations
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from hooze.api.deps import get_container, require_platform, require_tenant, require_tenant_admin
from hooze.capabilities.booking import BookingCapability
from hooze.capabilities.handoff import set_ai_paused
from hooze.config_engine.activation import activate
from hooze.config_engine.normalize import ConfigProblem, parse_config
from hooze.config_engine.schema import BusinessConfig
from hooze.config_engine.validator import validate
from hooze.kernel.db import first
from hooze.media.cleanup import sweep_after_save
from hooze.runtime.tenant_runtime import build_tenant_runtime
from hooze.security import new_secret, sha256_hex

router = APIRouter()


class TenantIn(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{2,60}$")
    name: str

class ChannelIn(BaseModel):
    kind: str = "whatsapp_evolution"
    instance_name: str
    base_url: str
    api_key: str

class PaystackIn(BaseModel):
    secret_key: str
    email_domain: str = "hooze-orders.com"

class ConfirmIn(BaseModel):
    when: str
    notes: Optional[str] = None


# ── platform ────────────────────────────────────────────────────
@router.post("/v1/platform/tenants", dependencies=[Depends(require_platform)])
async def create_tenant(body: TenantIn, request: Request):
    c = get_container(request)
    t = (await c.pdb.table("tenants").insert({"slug": body.slug, "name": body.name, "status": "onboarding"}).execute()).data[0]
    key = new_secret()
    await c.pdb.table("api_keys").insert({"tenant_id": t["id"], "key_hash": sha256_hex(key), "label": "initial", "role": "staff"}).execute()
    return {"tenant": t, "api_key": key, "note": "API key is shown once."}


@router.post("/v1/platform/tenants/{tenant_id}/channels", dependencies=[Depends(require_platform)])
async def create_channel(tenant_id: str, body: ChannelIn, request: Request):
    c = get_container(request)
    if not first(await c.pdb.table("tenants").select("id").eq("id", tenant_id).limit(1).execute()):
        raise HTTPException(404, "tenant not found")        # a bad id used to surface as a 500 (FK violation), not a 404
    secret = new_secret()
    ch = (await c.pdb.table("channels").insert({
        "tenant_id": tenant_id, "kind": body.kind, "instance_name": body.instance_name, "config": {"base_url": body.base_url},
        "credentials_enc": c.vault.encrypt(body.api_key), "secret_hash": sha256_hex(secret), "status": "active"}).execute()).data[0]
    base = c.settings.public_base_url.rstrip("/")
    return {"channel_id": ch["id"], "webhook_url": f"{base}/v1/webhooks/whatsapp/{ch['id']}",
            "webhook_header": {"x-hooze-secret": secret}, "note": "Set this URL + header as the Evolution webhook. The secret is shown once."}


# ── tenant: config lifecycle ────────────────────────────────────
async def _with_real_integration_flags(c, tenant_id: str, bc: BusinessConfig) -> BusinessConfig:
    """Never trust client-supplied 'connected' flags: derive them from actual database state.

    A client with the web test channel enabled (and no WhatsApp number yet) can go live for the TEST CHAT and DEMO LINKS only. Payments
    there are simulated, so a Paystack key is not required until a real WhatsApp number is connected."""
    ch = (await c.pdb.table("channels").select("id").eq("tenant_id", tenant_id).eq("status", "active").limit(1).execute()).data
    web = (await c.pdb.table("channels").select("id").eq("tenant_id", tenant_id).eq("kind", "web_demo").limit(1).execute()).data
    pay = (await c.pdb.table("tenant_integrations").select("id").eq("tenant_id", tenant_id).eq("status", "active").limit(1).execute()).data
    bc.integrations.channel_connected = bool(ch) or bool(web)
    bc.integrations.payments_connected = bool(pay) or (bool(web) and not ch)
    if pay and not bc.integrations.payments:
        bc.integrations.payments = "paystack"
    return bc


async def _report(c, tenant_id: str, bc: BusinessConfig) -> dict:
    """The readiness report, plus a plain warning when the client can only be reached on the web test chat (no WhatsApp number yet)."""
    out = validate(await _with_real_integration_flags(c, tenant_id, bc)).to_dict()
    wa = (await c.pdb.table("channels").select("id").eq("tenant_id", tenant_id).eq("status", "active").limit(1).execute()).data
    if not wa and bc.integrations.channel_connected:
        out["warnings"].append({"code": "integrations.whatsapp", "field": "integrations.channel_connected",
                                "message": "No WhatsApp number is paired yet. Going live now works for the test chat and demo links only; "
                                           "real customers can't reach the assistant until you pair a number."})
    return out


def _parse_or_422(raw: dict) -> tuple[BusinessConfig, list[str]]:
    """Repair near-miss configs (see config_engine/normalize.py); if it is still unusable, say exactly what is wrong."""
    try:
        return parse_config(raw)
    except ConfigProblem as e:
        raise HTTPException(422, {"message": "The configuration is not valid: " + "; ".join(e.problems), "problems": e.problems})


# ── drafts: save setup work WITHOUT going live ──────────────────
# Onboarding used to have exactly one save button: "Go live" — which is blocked until WhatsApp (and, for order
# tenants, Paystack) is connected. So a half-finished setup lived only in the operator's browser tab and died with it.
# A draft is a tenant_configs row with status='draft' and version=0 (never a version activation can produce, which
# starts at 1), updated in place. Saving a draft requires nothing to be connected; going live still requires everything.

DRAFT_VERSION = 0


async def _draft_row(db) -> Optional[dict]:
    return first(await db.table("tenant_configs").select("*").eq("status", "draft").limit(1).execute())


def _draft_view(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return {"config": row["config"], "saved_at": row.get("updated_at") or row.get("created_at"), "validation_report": row.get("validation_report")}


@router.put("/v1/tenants/{tenant_id}/config/draft", dependencies=[Depends(require_tenant)])
async def save_draft(tenant_id: str, request: Request, raw: dict = Body(...)):
    """Save setup progress. Deliberately NOT gated on readiness: an incomplete config is exactly what a draft is for.
    The current readiness report is stored alongside it so the UI (or an AI interview) can show what is still missing."""
    c = get_container(request)
    bc, fixes = _parse_or_422(raw)
    report = await _report(c, tenant_id, bc)
    db = c.pdb.for_tenant(tenant_id)
    fields = {"config": bc.model_dump(mode="json"), "validation_report": report, "updated_at": c.clock().isoformat()}
    existing = await _draft_row(db)
    if existing:
        await db.table("tenant_configs").update(fields).eq("id", existing["id"]).execute()
    else:
        await db.table("tenant_configs").insert({**fields, "version": DRAFT_VERSION, "status": "draft", "compiled": {}}).execute()
    out = {"saved": True, "saved_at": fields["updated_at"], "report": report}
    if fixes:
        out["normalized"] = fixes
    return out


@router.delete("/v1/tenants/{tenant_id}/config/draft", dependencies=[Depends(require_tenant)])
async def discard_draft(tenant_id: str, request: Request):
    rows = (await get_container(request).pdb.for_tenant(tenant_id).table("tenant_configs").delete().eq("status", "draft").execute()).data
    return {"discarded": bool(rows)}


@router.post("/v1/tenants/{tenant_id}/config/validate", dependencies=[Depends(require_tenant)])
async def validate_config(tenant_id: str, request: Request, raw: dict = Body(...)):
    c = get_container(request)
    bc, fixes = _parse_or_422(raw)
    out = await _report(c, tenant_id, bc)
    if fixes:
        out["normalized"] = fixes          # what we repaired, so the UI can show it
    return out


@router.post("/v1/tenants/{tenant_id}/config/activate", dependencies=[Depends(require_tenant_admin)])
async def activate_config(tenant_id: str, request: Request, background: BackgroundTasks, raw: dict = Body(...)):
    c = get_container(request)
    bc, _fixes = _parse_or_422(raw)
    res = await activate(c.pdb, c.llm, tenant_id, await _with_real_integration_flags(c, tenant_id, bc), configs=c.configs)
    if not res.ok:
        if res.retryable:
            raise HTTPException(503, {"message": res.error, "retryable": True, "seeded": res.seeded})     # a dependency failed, not the config
        raise HTTPException(422, {"message": "configuration is not ready to go live", "report": res.report.to_dict()})
    await c.pdb.table("tenants").update({"status": "active"}).eq("id", tenant_id).execute()
    if hasattr(c.pipeline, "forget_tenant"):
        c.pipeline.forget_tenant(tenant_id)
    try:        # the draft just became the live version; a leftover draft would shadow it on the next Setup load
        await c.pdb.for_tenant(tenant_id).table("tenant_configs").delete().eq("status", "draft").execute()
    except Exception as e:
        await c.events.warn("DRAFT_CLEANUP_FAILED", tenant_id=tenant_id, error=str(e)[:160])
    await c.events.info("TENANT_ACTIVATED", tenant_id=tenant_id, version=res.version)
    background.add_task(sweep_after_save, c, tenant_id)          # delete uploaded files no live menu item uses any more (old enough only)
    return {"activated": True, "version": res.version, "seeded": res.seeded, "report": res.report.to_dict()}


@router.post("/v1/tenants/{tenant_id}/integrations/paystack", dependencies=[Depends(require_tenant_admin)])
async def connect_paystack(tenant_id: str, body: PaystackIn, request: Request):
    c = get_container(request)
    row = {"tenant_id": tenant_id, "kind": "paystack", "credentials_enc": c.vault.encrypt(body.secret_key), "config": {"email_domain": body.email_domain}, "status": "active"}
    existing = first(await c.pdb.table("tenant_integrations").select("id").eq("tenant_id", tenant_id).eq("kind", "paystack").limit(1).execute())
    if existing:
        await c.pdb.table("tenant_integrations").update(row).eq("id", existing["id"]).execute()
    else:
        await c.pdb.table("tenant_integrations").insert(row).execute()
    base = c.settings.public_base_url.rstrip("/")
    return {"connected": True, "webhook_url": f"{base}/v1/webhooks/payments/{tenant_id}/paystack"}


# ── tenant: operations ──────────────────────────────────────────
@router.get("/v1/tenants/{tenant_id}/conversations", dependencies=[Depends(require_tenant)])
async def list_conversations(tenant_id: str, request: Request, status: Optional[str] = None, limit: int = 50):
    c = get_container(request)
    db = c.pdb.for_tenant(tenant_id)
    q = db.table("conversations").select("*")
    if status:
        q = q.eq("status", status)
    rows = (await q.order("updated_at", desc=True).limit(min(max(limit, 1), 200)).execute()).data or []
    from hooze.api.operator import contacts_by_id          # lazy: operator imports admin-level helpers too
    contacts = await contacts_by_id(db, [r["contact_id"] for r in rows])
    return {"conversations": [{**r, "contact": contacts.get(r["contact_id"])} for r in rows]}


@router.post("/v1/tenants/{tenant_id}/conversations/{conversation_id}/{action}", dependencies=[Depends(require_tenant)])
async def pause_resume(tenant_id: str, conversation_id: str, action: str, request: Request):
    if action not in ("pause", "resume"):
        raise HTTPException(404, "not found")
    db = get_container(request).pdb.for_tenant(tenant_id)
    conv = first(await db.table("conversations").select("*").eq("id", conversation_id).limit(1).execute())
    if not conv:
        raise HTTPException(404, "not found")
    paused = action == "pause"
    try:
        await set_ai_paused(db, conv["contact_id"], paused, conversation_id=conversation_id)
    except Exception as e:
        await get_container(request).events.error("HANDOFF_TOGGLE_FAILED", tenant_id=tenant_id, conversation=conversation_id, paused=paused, error=str(e)[:200])
        raise HTTPException(500, f"Couldn't {action} this chat. Nothing was confirmed — try again.")
    return {"status": "handoff" if paused else "active"}


@router.get("/v1/tenants/{tenant_id}/bookings", dependencies=[Depends(require_tenant)])
async def list_bookings(tenant_id: str, request: Request, status: str = "pending"):
    db = get_container(request).pdb.for_tenant(tenant_id)
    q = db.table("bookings").select("*")
    if status != "all":
        q = q.eq("status", status)
    rows = (await q.order("created_at", desc=True).limit(200).execute()).data or []
    from hooze.api.operator import contacts_by_id
    contacts = await contacts_by_id(db, [r["contact_id"] for r in rows])
    return {"bookings": [{**r, "contact": contacts.get(r["contact_id"])} for r in rows]}


async def _booking_cap(c, tenant_id: str) -> BookingCapability:
    cfg = await c.configs.load_active(tenant_id)
    if not cfg or "booking" not in cfg.capabilities:
        raise HTTPException(409, "booking is not enabled for this tenant")
    _, caps = build_tenant_runtime(pdb=c.pdb, adapters=c.adapters, events=c.events, llm=c.llm, cfg=cfg, tenant_id=tenant_id, clock=c.clock)
    return caps["booking"]


@router.post("/v1/tenants/{tenant_id}/bookings/{booking_id}/confirm", dependencies=[Depends(require_tenant)])
async def confirm_booking(tenant_id: str, booking_id: str, body: ConfirmIn, request: Request):
    cap = await _booking_cap(get_container(request), tenant_id)
    try:
        row = await cap.confirm(booking_id, body.when, body.notes)
    except ValueError:
        raise HTTPException(422, "`when` must be an ISO-8601 datetime")
    if not row:
        raise HTTPException(409, "booking not found or already decided")
    return {"status": "confirmed"}


@router.post("/v1/tenants/{tenant_id}/bookings/{booking_id}/decline", dependencies=[Depends(require_tenant)])
async def decline_booking(tenant_id: str, booking_id: str, request: Request, body: Optional[ConfirmIn] = None):
    cap = await _booking_cap(get_container(request), tenant_id)
    if not await cap.decline(booking_id, body.notes if body else None):
        raise HTTPException(409, "booking not found or already decided")
    return {"status": "declined"}


@router.get("/v1/tenants/{tenant_id}/usage", dependencies=[Depends(require_tenant)])
async def usage(tenant_id: str, request: Request, since: Optional[str] = None):
    """Per-tenant metered usage: what a billing job (or the customer's dashboard) reads. `since` is an ISO timestamp.

    Totals are exact. This used to fetch every row in one request, and Supabase returns at most 1,000 rows per request, so any client
    past 1,000 messages (or AI calls) was silently UNDER-counted — the number a bill would have been based on."""
    if since:
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "`since` must be an ISO-8601 date or datetime, e.g. 2026-11-01")
    db = get_container(request).pdb.for_tenant(tenant_id)
    totals: dict[str, float] = {}
    page = 1000
    for start in range(0, page * 500, page):                    # pages through up to 500k usage rows
        b = db.table("usage_events").select("kind, quantity")      # oldest first: rows written while paging land at the end, not mid-page
        rows = (await (b.gte("created_at", since) if since else b).order("created_at").range(start, start + page - 1).execute()).data or []
        for r in rows:
            totals[r["kind"]] = totals.get(r["kind"], 0) + float(r["quantity"])
        if len(rows) < page:
            break

    async def n_messages(**eq) -> int:
        b = db.table("messages").select("id", count="exact")
        for k, v in eq.items():
            b = b.eq(k, v)
        return int((await (b.gte("created_at", since) if since else b).limit(1).execute()).count or 0)
    return {"since": since, "metered": totals,
            "messages_in": await n_messages(direction="inbound"),
            "messages_out_bot": await n_messages(direction="outbound", sender_type="bot")}

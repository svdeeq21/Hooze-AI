"""Operator API: everything the dashboard needs that a command-line workflow never did.

Auth model (unchanged): the PLATFORM key (operator) can call everything here; a TENANT key can call only the /v1/tenants/{id}/...
routes for its own tenant (so the same endpoints will back a client-facing dashboard later). Platform-wide routes are platform-only.

Registered BEFORE `admin.router` so literal paths (e.g. `.../conversations/{id}/send`) win over the generic
`.../conversations/{id}/{action}` pause/resume route.
"""
from __future__ import annotations
import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from hooze.config_engine.activation import reindex_knowledge
from hooze.api.deps import get_container, require_platform, require_tenant, require_tenant_admin
from hooze.channels.evolution import create_instance as create_evolution_instance, delete_instance as delete_evolution_instance
from hooze.kernel.db import first
from hooze.media.cleanup import purge_tenant, sweep_tenant
from hooze.media.processing import MediaError, process as process_media
from hooze.media.storage import MediaStorageError
from hooze.privacy import erase_customer, export_customer
from hooze.runtime.merge import merge_tenant_conversations
from hooze.runtime.scheduler import run_tick
from hooze.runtime.store import ConversationStore
from hooze.runtime.tenant_runtime import build_tenant_runtime
from hooze.security import new_secret, sha256_hex

router = APIRouter()

ORDER_STATUSES = ["CART", "PAYMENT_PENDING", "PAID", "PREPARING", "READY", "COMPLETED", "CANCELLED", "EXPIRED"]
PAID_STATUSES = ["PAID", "PREPARING", "READY", "COMPLETED"]
# what staff may move an order to, from where (conditional UPDATE, so two staff clicking at once cannot both win)
ORDER_FLOW = {"PAID": {"PREPARING", "READY", "COMPLETED", "CANCELLED"}, "PREPARING": {"READY", "COMPLETED", "CANCELLED"}, "READY": {"COMPLETED", "CANCELLED"}}


# ── helpers ─────────────────────────────────────────────────────────────
async def _count(q) -> int:
    res = await q.limit(1).execute()
    return int(res.count or 0)


def _contact_view(c: Optional[dict]) -> Optional[dict]:
    if not c:
        return None
    return {"id": c["id"], "phone": c["phone"], "display_name": c.get("display_name"), "ai_paused": c.get("ai_paused", False),
            "opted_out": c.get("opted_out", False), "slots": (c.get("attributes") or {}).get("slots") or {}}


async def contacts_by_id(db, ids: list[str]) -> dict[str, dict]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    rows = (await db.table("contacts").select("*").in_("id", ids).execute()).data or []
    return {r["id"]: _contact_view(r) for r in rows}


def _channel_public(ch: dict) -> dict:
    """Never expose credentials_enc or secret_hash."""
    return {"id": ch["id"], "kind": ch["kind"], "instance_name": ch["instance_name"], "status": ch["status"],
            "base_url": (ch.get("config") or {}).get("base_url"), "created_at": ch.get("created_at")}


async def _channel_state(c, ch: dict) -> str:
    try:
        return await asyncio.wait_for(c.adapters.for_channel(ch).connection_status(), 6.0)
    except Exception:
        return "UNKNOWN"


async def _tenant_or_404(c, tenant_id: str) -> dict:
    t = first(await c.pdb.table("tenants").select("*").eq("id", tenant_id).limit(1).execute())
    if not t:
        raise HTTPException(404, "tenant not found")
    return t


async def _channel_or_404(c, tenant_id: str, channel_id: str) -> dict:
    ch = first(await c.pdb.table("channels").select("*").eq("id", channel_id).eq("tenant_id", tenant_id).limit(1).execute())
    if not ch:
        raise HTTPException(404, "channel not found")
    return ch


# ═══════════════════════ platform: health & fleet ═══════════════════════
@router.get("/v1/platform/overview", dependencies=[Depends(require_platform)])
async def overview(request: Request):
    c = get_container(request)
    tenants = (await c.pdb.table("tenants").select("*").execute()).data or []
    issues = (await c.pdb.table("events").select("*").in_("severity", ["WARNING", "ERROR", "CRITICAL"]).order("created_at", desc=True).limit(15).execute()).data or []
    llm = c.llm.status() if hasattr(c.llm, "status") else []
    s = c.settings
    inbound = await c.inbox.counts() if c.inbox else {"pending": 0, "processing": 0, "failed": 0}
    return {
        "alerts": {"configured": bool(c.alerts and c.alerts.sinks), "destinations": c.alerts.describe() if c.alerts else []},
        "inbound": inbound,
        "tenants": {"total": len(tenants), **{k: sum(1 for t in tenants if t["status"] == k) for k in ("active", "onboarding", "suspended")}},
        "llm_providers": llm,
        "recent_issues": issues,
        "platform": {"app_env": s.app_env, "public_base_url": s.public_base_url, "nudges_globally_enabled": s.nudges_globally_enabled,
                     "shared_state": "redis" if s.upstash_redis_rest_url else "in-process (run ONE worker)", "llm_timeout_seconds": s.llm_timeout_seconds},
    }


@router.get("/v1/platform/tenants", dependencies=[Depends(require_platform)])
async def list_tenants(request: Request):
    c = get_container(request)
    tenants = (await c.pdb.table("tenants").select("*").order("created_at", desc=True).execute()).data or []
    channels = (await c.pdb.table("channels").select("id, tenant_id, status").execute()).data or []
    active = (await c.pdb.table("tenant_configs").select("tenant_id, version, activated_at").eq("status", "active").execute()).data or []
    by_cfg = {r["tenant_id"]: r for r in active}
    counts = {r["tenant_id"]: r for r in ((await c.pdb.rpc("platform_conversation_counts").execute()).data or [])}      # one query, not 2 per client
    out = []
    for t in tenants:
        tid = t["id"]
        cc = counts.get(tid) or {}
        out.append({**t, "channels": sum(1 for ch in channels if ch["tenant_id"] == tid and ch["status"] == "active"),
                    "active_version": (by_cfg.get(tid) or {}).get("version"), "conversations": int(cc.get("total", 0)), "handoff": int(cc.get("handoff", 0))})
    return {"tenants": out}


@router.get("/v1/platform/tenants/{tenant_id}", dependencies=[Depends(require_platform)])
async def tenant_detail(tenant_id: str, request: Request):
    c = get_container(request)
    t = await _tenant_or_404(c, tenant_id)
    chans = (await c.pdb.table("channels").select("*").eq("tenant_id", tenant_id).eq("status", "active").order("created_at").execute()).data or []
    states = await asyncio.gather(*[_channel_state(c, ch) for ch in chans])
    keys = (await c.pdb.table("api_keys").select("id, label, role, revoked_at, created_at").eq("tenant_id", tenant_id).order("created_at").execute()).data or []
    db = c.pdb.for_tenant(tenant_id)
    integ = (await db.table("tenant_integrations").select("id, kind, status, config, created_at").execute()).data or []
    cfg = first(await db.table("tenant_configs").select("*").eq("status", "active").limit(1).execute())
    cfg_out = {"version": cfg["version"], "activated_at": cfg.get("activated_at"), "config": cfg["config"], "validation_report": cfg.get("validation_report")} if cfg else None
    base = c.settings.public_base_url.rstrip("/")
    return {"tenant": t, "channels": [{**_channel_public(ch), "connection": st, "webhook_url": f"{base}/v1/webhooks/whatsapp/{ch['id']}"} for ch, st in zip(chans, states)],
            "api_keys": keys, "integrations": [{**i, "payment_webhook_url": f"{base}/v1/webhooks/payments/{tenant_id}/{i['kind']}"} for i in integ], "active_config": cfg_out}


class TenantPatch(BaseModel):
    status: Optional[str] = Field(None, pattern="^(active|suspended)$")
    name: Optional[str] = Field(None, min_length=1, max_length=120)


@router.patch("/v1/platform/tenants/{tenant_id}", dependencies=[Depends(require_platform)])
async def patch_tenant(tenant_id: str, body: TenantPatch, request: Request):
    c = get_container(request)
    await _tenant_or_404(c, tenant_id)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(422, "nothing to change")
    row = first(await c.pdb.table("tenants").update(fields).eq("id", tenant_id).execute())
    await c.events.info("TENANT_UPDATED", tenant_id=tenant_id, **fields)
    return {"tenant": row}


@router.post("/v1/platform/embeddings/reindex", dependencies=[Depends(require_platform)])
async def reindex_embeddings(request: Request, tenant_id: Optional[str] = None):
    """Re-embed knowledge (FAQs, listings, menu facts) with the CURRENT embedding model, for one client or for all of them.
    Run this once after changing EMBEDDING_MODEL (or when Google retires a model): vectors from different models cannot be compared, so
    until it has run, lookups match the wrong things or nothing. Safe to repeat: documents already up to date are skipped."""
    c = get_container(request)
    ids = [tenant_id] if tenant_id else [t["id"] for t in (await c.pdb.table("tenants").select("id").execute()).data or []]
    out = {}
    for tid in ids:
        try:
            out[tid] = await reindex_knowledge(c.pdb, c.llm, tid)
        except Exception as e:
            out[tid] = {"status": "error", "error": str(e)[:200]}
    await c.events.info("EMBEDDINGS_REINDEXED", tenants=len(ids), failed=sum(1 for r in out.values() if r["status"] in ("failed", "error")))
    return {"model": c.llm.embedding_recipe() if hasattr(c.llm, "embedding_recipe") else "", "tenants": out}


@router.post("/v1/platform/tenants/{tenant_id}/conversations/merge", dependencies=[Depends(require_platform)])
async def merge_conversations(tenant_id: str, request: Request, dry_run: bool = True):
    """Join customers' duplicate conversations (one per number, from before chats became one-per-customer). Dry run by default."""
    c = get_container(request)
    await _tenant_or_404(c, tenant_id)
    r = await merge_tenant_conversations(c.pdb.for_tenant(tenant_id), dry_run=dry_run)
    await c.events.info("CONVERSATIONS_MERGED", tenant_id=tenant_id, dry_run=dry_run, customers=r["customers_with_duplicates"], removed=r["conversations_removed"])
    return r


@router.post("/v1/platform/tenants/{tenant_id}/media/cleanup", dependencies=[Depends(require_platform)])
async def cleanup_media(tenant_id: str, request: Request, dry_run: bool = True):
    """Delete uploaded files no live menu item uses (older than the age gate). Defaults to a DRY RUN that only lists what would go."""
    c = get_container(request)
    await _tenant_or_404(c, tenant_id)
    if c.media is None:
        raise HTTPException(503, "Media storage isn't set up on this server.")
    try:
        r = await sweep_tenant(c.pdb, c.media, tenant_id, now=c.clock(), min_age_hours=c.settings.media_orphan_min_age_hours, dry_run=dry_run)
    except MediaStorageError as e:
        raise HTTPException(502, f"Storage error: {str(e)[:160]}")
    await c.events.info("MEDIA_CLEANUP_MANUAL", tenant_id=tenant_id, dry_run=dry_run, deleted=r["deleted"], would_delete=len(r["would_delete"]))
    return {"dry_run": dry_run, **r}


@router.delete("/v1/platform/tenants/{tenant_id}", dependencies=[Depends(require_platform)])
async def delete_tenant(tenant_id: str, request: Request, confirm: str = ""):
    """PERMANENTLY remove a client: every row (cascade) AND every uploaded file. Deliberately hard to do by accident:
    the client must already be suspended, and `confirm` must equal its slug. Files go first; if that fails nothing is deleted."""
    c = get_container(request)
    t = await _tenant_or_404(c, tenant_id)
    if t["status"] != "suspended":
        raise HTTPException(409, "Pause (suspend) this client first, then delete.")
    if confirm != t["slug"]:
        raise HTTPException(422, f"To confirm, repeat the client's slug: {t['slug']}")
    files = 0
    if c.media is not None:
        try:
            files = await purge_tenant(c.media, tenant_id)
        except MediaStorageError as e:
            await c.events.error("TENANT_DELETE_FILES_FAILED", tenant_id=tenant_id, error=str(e)[:200])
            raise HTTPException(502, "Couldn't delete this client's files from storage, so nothing was deleted. Try again.")
    # The client's WhatsApp numbers: remove each instance from the Evolution server so it is not left running (and billed) with nobody using it.
    # A dead/unreachable Evolution server must not make deletion impossible, so failures are REPORTED (to remove by hand) rather than blocking.
    instances, failed = 0, []
    for ch in (await c.pdb.table("channels").select("*").eq("tenant_id", tenant_id).execute()).data or []:
        try:
            key = c.vault.decrypt(ch["credentials_enc"]) if ch.get("credentials_enc") else ""
            await delete_evolution_instance((ch.get("config") or {}).get("base_url", ""), key, ch["instance_name"])
            instances += 1
        except Exception as e:
            failed.append({"instance": ch.get("instance_name"), "error": str(e)[:160]})
    await c.pdb.table("tenants").delete().eq("id", tenant_id).execute()
    c.configs.invalidate(tenant_id)
    await c.events.warn("TENANT_DELETED", slug=t["slug"], name=t["name"], deleted_tenant_id=tenant_id, files_deleted=files,
                        instances_deleted=instances, instances_failed=failed)                    # no tenant_id: the row is gone
    return {"deleted": True, "slug": t["slug"], "files_deleted": files, "instances_deleted": instances, "instances_to_remove_by_hand": failed}


class KeyIn(BaseModel):
    label: str = Field("operator", max_length=80)
    role: str = Field("staff", pattern="^(admin|staff|viewer)$")


@router.post("/v1/platform/tenants/{tenant_id}/api-keys", dependencies=[Depends(require_platform)])
async def create_key(tenant_id: str, body: KeyIn, request: Request):
    c = get_container(request)
    await _tenant_or_404(c, tenant_id)
    key = new_secret()
    row = first(await c.pdb.table("api_keys").insert({"tenant_id": tenant_id, "key_hash": sha256_hex(key), "label": body.label, "role": body.role}).execute())
    return {"id": row["id"], "label": body.label, "role": body.role, "api_key": key, "note": "Shown once."}


@router.delete("/v1/platform/tenants/{tenant_id}/api-keys/{key_id}", dependencies=[Depends(require_platform)])
async def revoke_key(tenant_id: str, key_id: str, request: Request):
    c = get_container(request)
    rows = (await c.pdb.table("api_keys").update({"revoked_at": c.clock().isoformat()}).eq("id", key_id).eq("tenant_id", tenant_id).execute()).data
    if not rows:
        raise HTTPException(404, "key not found")
    return {"revoked": True}


@router.post("/v1/platform/tick", dependencies=[Depends(require_platform)])
async def run_tick_now(request: Request):
    """Same sweep as POST /internal/tick, behind the operator key so the dashboard can trigger it (dry-run unless a tenant's nudges are live)."""
    c = get_container(request)
    out = await run_tick(pdb=c.pdb, configs=c.configs, adapters=c.adapters, events=c.events, llm=c.llm, clock=c.clock, extras_for=c.extras_for)
    if c.maintenance:
        out["maintenance"] = await c.maintenance.run_once()
    return out


@router.get("/v1/platform/events", dependencies=[Depends(require_platform)])
async def list_events(request: Request, tenant_id: Optional[str] = None, severity: Optional[str] = None, event: Optional[str] = None, limit: int = 100):
    c = get_container(request)
    q = c.pdb.table("events").select("*")
    if tenant_id:
        q = q.eq("tenant_id", tenant_id)
    if severity:
        q = q.in_("severity", [s.strip().upper() for s in severity.split(",") if s.strip()])
    if event:
        q = q.eq("event", event)
    return {"events": (await q.order("created_at", desc=True).limit(min(max(limit, 1), 500)).execute()).data or []}


# ═══════════════════════ platform: queue + alerts ═══════════════════════
@router.post("/v1/platform/alerts/test", dependencies=[Depends(require_platform)])
async def test_alert(request: Request):
    """Send a test alert to every configured destination so you KNOW alerts reach you before you need them."""
    c = get_container(request)
    if not (c.alerts and c.alerts.sinks):
        return {"configured": False, "results": []}
    return {"configured": True, "results": await c.alerts.send_test()}


@router.get("/v1/platform/inbound", dependencies=[Depends(require_platform)])
async def list_inbound(request: Request, status: str = "failed", limit: int = 50):
    """Messages the queue is holding. Customer text (the payload) is deliberately NOT returned here."""
    c = get_container(request)
    rows = (await c.pdb.table("inbound_events").select("id, tenant_id, channel_id, provider_message_id, status, attempts, last_error, received_at, finished_at")
            .in_("status", [x.strip() for x in status.split(",")]).order("received_at", desc=True).limit(min(max(limit, 1), 200)).execute()).data or []
    names = {t["id"]: t["name"] for t in ((await c.pdb.table("tenants").select("id, name").execute()).data or [])}
    keep = ("id", "tenant_id", "channel_id", "provider_message_id", "status", "attempts", "last_error", "received_at", "finished_at")
    return {"inbound": [{**{k: r.get(k) for k in keep}, "tenant_name": names.get(r["tenant_id"])} for r in rows]}      # explicit fields: never the customer's text


@router.post("/v1/platform/inbound/{event_id}/retry", dependencies=[Depends(require_platform)])
async def retry_inbound(event_id: str, request: Request, background: BackgroundTasks):
    c = get_container(request)
    if not await c.inbox.retry(event_id):
        raise HTTPException(404, "no failed message with that id")
    background.add_task(c.inbox.process, event_id)
    return {"retrying": True}


# ═══════════════════════ platform: WhatsApp channel ═══════════════════════
class ProvisionIn(BaseModel):
    instance_name: str = Field(min_length=2, max_length=80)
    base_url: str
    api_key: str
    create_instance: bool = False          # also create the instance on Evolution (otherwise it must already exist)
    webhook_base_url: Optional[str] = None  # how EVOLUTION reaches hooze-core (e.g. http://host.docker.internal:8000); defaults to PUBLIC_BASE_URL


async def _set_webhook(c, channel: dict, secret: str, webhook_base: str) -> str:
    url = f"{webhook_base.rstrip('/')}/v1/webhooks/whatsapp/{channel['id']}"
    await c.adapters.for_channel(channel).set_webhook(url, {"x-hooze-secret": secret})
    return url


@router.post("/v1/platform/tenants/{tenant_id}/channels/provision", dependencies=[Depends(require_platform)])
async def provision_channel(tenant_id: str, body: ProvisionIn, request: Request):
    """One call: (optionally create the Evolution instance) -> channel row -> webhook pointed at us with a fresh secret."""
    c = get_container(request)
    await _tenant_or_404(c, tenant_id)
    if body.create_instance:
        try:
            await create_evolution_instance(body.base_url, body.api_key, body.instance_name)
        except Exception as e:
            raise HTTPException(502, f"could not create the Evolution instance: {str(e)[:200]}")
    secret = new_secret()
    ch = first(await c.pdb.table("channels").insert({
        "tenant_id": tenant_id, "kind": "whatsapp_evolution", "instance_name": body.instance_name, "config": {"base_url": body.base_url},
        "credentials_enc": c.vault.encrypt(body.api_key), "secret_hash": sha256_hex(secret), "status": "active"}).execute())
    base = body.webhook_base_url or c.settings.public_base_url
    try:
        url = await _set_webhook(c, ch, secret, base)
        wired = True
    except Exception as e:
        url, wired = f"{base.rstrip('/')}/v1/webhooks/whatsapp/{ch['id']}", False
        await c.events.warn("WEBHOOK_SET_FAILED", tenant_id=tenant_id, channel_id=ch["id"], error=str(e)[:200])
    return {"channel": _channel_public(ch), "webhook_url": url, "webhook_configured": wired,
            "note": None if wired else "Channel saved, but Evolution could not be pointed at us automatically. Check the instance exists and the URL is reachable from Evolution, then use 'Reconnect webhook'."}


@router.get("/v1/platform/tenants/{tenant_id}/channels/{channel_id}/status", dependencies=[Depends(require_platform)])
async def channel_status(tenant_id: str, channel_id: str, request: Request):
    c = get_container(request)
    return {"connection": await _channel_state(c, await _channel_or_404(c, tenant_id, channel_id))}


@router.get("/v1/platform/tenants/{tenant_id}/channels/{channel_id}/qr", dependencies=[Depends(require_platform)])
async def channel_qr(tenant_id: str, channel_id: str, request: Request):
    c = get_container(request)
    ch = await _channel_or_404(c, tenant_id, channel_id)
    try:
        return await c.adapters.for_channel(ch).fetch_qr()
    except Exception as e:
        raise HTTPException(502, f"Evolution did not return a QR code: {str(e)[:200]}")


@router.delete("/v1/platform/tenants/{tenant_id}/channels/{channel_id}", dependencies=[Depends(require_platform)])
async def remove_channel(tenant_id: str, channel_id: str, request: Request):
    """Retire a WhatsApp number (e.g. a stale test instance): it stops receiving, stops being monitored, and disappears from the dashboard.
    The row is kept (status 'disabled') so the customers' message history that points at it stays intact."""
    c = get_container(request)
    await _channel_or_404(c, tenant_id, channel_id)
    await c.pdb.table("channels").update({"status": "disabled"}).eq("id", channel_id).eq("tenant_id", tenant_id).execute()
    await c.events.info("CHANNEL_REMOVED", tenant_id=tenant_id, channel_id=channel_id)
    return {"removed": True}


class WebhookIn(BaseModel):
    webhook_base_url: Optional[str] = None


@router.post("/v1/platform/tenants/{tenant_id}/channels/{channel_id}/webhook", dependencies=[Depends(require_platform)])
async def reconnect_webhook(tenant_id: str, channel_id: str, body: WebhookIn, request: Request):
    """The webhook secret is stored only as a hash, so re-pointing Evolution means ROTATING it. Order matters: set Evolution first,
    and only store the new hash once that worked, so a failure leaves the old (working) pair intact."""
    c = get_container(request)
    ch = await _channel_or_404(c, tenant_id, channel_id)
    secret = new_secret()
    try:
        url = await _set_webhook(c, ch, secret, body.webhook_base_url or c.settings.public_base_url)
    except Exception as e:
        raise HTTPException(502, f"Evolution rejected the webhook update: {str(e)[:200]}")
    await c.pdb.table("channels").update({"secret_hash": sha256_hex(secret)}).eq("id", channel_id).eq("tenant_id", tenant_id).execute()
    await c.events.info("WEBHOOK_ROTATED", tenant_id=tenant_id, channel_id=channel_id)
    return {"webhook_url": url, "configured": True}


# ═══════════════════════ tenant: config, stats, conversations ═══════════════════════
@router.get("/v1/tenants/{tenant_id}/config", dependencies=[Depends(require_tenant)])
async def get_config(tenant_id: str, request: Request):
    db = get_container(request).pdb.for_tenant(tenant_id)
    cfg = first(await db.table("tenant_configs").select("*").eq("status", "active").limit(1).execute())
    if not cfg:
        return {"active": None}
    return {"active": {"version": cfg["version"], "activated_at": cfg.get("activated_at"), "config": cfg["config"], "validation_report": cfg.get("validation_report")}}


@router.get("/v1/tenants/{tenant_id}/stats", dependencies=[Depends(require_tenant)])
async def stats(tenant_id: str, request: Request):
    """One aggregate query (migrations/0004). It used to be ~25 separate COUNTs per refresh."""
    c = get_container(request)
    db = c.pdb.for_tenant(tenant_id)
    res = (await db.rpc("tenant_stats", {"p_since24": (c.clock() - timedelta(hours=24)).isoformat(), "p_since7d": (c.clock() - timedelta(days=7)).isoformat()}).execute()).data
    d = (res[0] if isinstance(res, list) and res else res) or {}
    zero = lambda keys, got: {k: int((got or {}).get(k, 0)) for k in keys}          # the SQL only returns statuses that exist; the API shape is fixed
    return {
        "conversations": zero(["active", "handoff", "closed"], d.get("conversations")),
        "messages_24h": {"inbound": int(d.get("inbound_24h", 0)), "bot_replies": int(d.get("bot_24h", 0))},
        "orders": zero(ORDER_STATUSES, d.get("orders")),
        "bookings": zero(["pending", "confirmed", "declined", "cancelled"], d.get("bookings")),
        "revenue_minor": {"total": int(d.get("revenue_total", 0)), "last_7d": int(d.get("revenue_7d", 0)), "truncated": False},
    }


@router.get("/v1/tenants/{tenant_id}/conversations/{conversation_id}/messages", dependencies=[Depends(require_tenant)])
async def conversation_messages(tenant_id: str, conversation_id: str, request: Request, limit: int = 100):
    db = get_container(request).pdb.for_tenant(tenant_id)
    conv = first(await db.table("conversations").select("*").eq("id", conversation_id).limit(1).execute())
    if not conv:
        raise HTTPException(404, "not found")
    rows = (await db.table("messages").select("id, direction, sender_type, content, content_type, created_at, meta").eq("conversation_id", conversation_id)
            .order("created_at", desc=True).limit(min(max(limit, 1), 500)).execute()).data or []
    contact = (await contacts_by_id(db, [conv["contact_id"]])).get(conv["contact_id"])
    return {"conversation": conv, "contact": contact, "messages": list(reversed(rows))}


class SendIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/v1/tenants/{tenant_id}/conversations/{conversation_id}/send", dependencies=[Depends(require_tenant)])
async def send_as_human(tenant_id: str, conversation_id: str, body: SendIn, request: Request):
    """A person takes over: sends from the business's own WhatsApp, records it as `human`, and PAUSES the assistant so it can't talk over them."""
    c = get_container(request)
    db = c.pdb.for_tenant(tenant_id)
    conv = first(await db.table("conversations").select("*").eq("id", conversation_id).limit(1).execute())
    contact = conv and first(await db.table("contacts").select("*").eq("id", conv["contact_id"]).limit(1).execute())
    ch = conv and first(await c.pdb.table("channels").select("*").eq("id", conv["channel_id"]).eq("tenant_id", tenant_id).limit(1).execute())
    if not (conv and contact and ch):
        raise HTTPException(404, "not found")
    try:
        await c.adapters.for_channel(ch).send_text(contact["phone"], body.text.strip(), typing_delay=0.0)
    except Exception as e:
        raise HTTPException(502, f"WhatsApp send failed: {str(e)[:200]}")
    store = ConversationStore(db, c.clock)
    try:
        await store.record_outbound(conversation_id, body.text.strip(), "human")
        await store.set_contact(contact["id"], ai_paused=True)
        await store.set_conversation(conversation_id, status="handoff")
    except Exception as e:      # the message WAS sent; never report failure (the operator would resend and the customer get it twice)
        await c.events.warn("HUMAN_SEND_RECORD_FAILED", tenant_id=tenant_id, error=str(e)[:160])
    return {"sent": True, "assistant_paused": True}


@router.post("/v1/tenants/{tenant_id}/nudges/reset", dependencies=[Depends(require_tenant_admin)])
async def reset_nudges(tenant_id: str, request: Request):
    """Run BEFORE switching follow-ups from dry-run to live: dry-run advances each conversation's step, which would skip early templates.
    Opted-out / lost contacts are left alone (never re-enrol someone who said stop)."""
    db = get_container(request).pdb.for_tenant(tenant_id)
    rows = (await db.table("nudge_state").update({"status": "ACTIVE", "step": 0, "last_nudge_at": None, "next_nudge_at": None})
            .in_("status", ["ACTIVE", "IDLE", "EXHAUSTED"]).execute()).data or []
    return {"reset": len(rows)}


# ═══════════════════════ customer privacy ═══════════════════════
@router.get("/v1/tenants/{tenant_id}/customers/{phone}/export", dependencies=[Depends(require_tenant_admin)])
async def export_customer_data(tenant_id: str, phone: str, request: Request):
    """Everything held about one customer (a data-access request). Admin or platform key only."""
    c = get_container(request)
    data = await export_customer(c.pdb.for_tenant(tenant_id), phone)
    if data is None:
        raise HTTPException(404, "No customer with that phone number.")
    await c.events.info("CUSTOMER_DATA_EXPORTED", tenant_id=tenant_id)
    return data


@router.delete("/v1/tenants/{tenant_id}/customers/{phone}", dependencies=[Depends(require_tenant_admin)])
async def erase_customer_data(tenant_id: str, phone: str, request: Request, dry_run: bool = True):
    """Erase one customer's personal data (a deletion request). Dry run by default. Orders and payments are KEPT as the business's records,
    with the person's details removed from them. Cannot be undone."""
    c = get_container(request)
    r = await erase_customer(c.pdb.for_tenant(tenant_id), phone, dry_run=dry_run)
    if r is None:
        raise HTTPException(404, "No customer with that phone number.")
    if not dry_run:
        await c.events.warn("CUSTOMER_ERASED", tenant_id=tenant_id, **{k: v for k, v in r.items() if k != "dry_run"})      # counts only, never the number
    return r


# ═══════════════════════ tenant: orders ═══════════════════════
@router.get("/v1/tenants/{tenant_id}/orders", dependencies=[Depends(require_tenant)])
async def list_orders(tenant_id: str, request: Request, status: Optional[str] = None, limit: int = 100):
    db = get_container(request).pdb.for_tenant(tenant_id)
    q = db.table("orders").select("*")
    if status and status != "all":
        q = q.in_("status", [s.strip().upper() for s in status.split(",")])
    else:
        q = q.in_("status", [s for s in ORDER_STATUSES if s != "CART"])      # carts are not orders yet
    rows = (await q.order("created_at", desc=True).limit(min(max(limit, 1), 200)).execute()).data or []
    contacts = await contacts_by_id(db, [r["contact_id"] for r in rows])
    return {"orders": [{**{k: r.get(k) for k in ("id", "status", "subtotal_minor", "delivery_fee_minor", "fulfillment_method", "delivery_address", "paid_at", "created_at", "updated_at")},
                        "contact": contacts.get(r["contact_id"])} for r in rows]}


@router.get("/v1/tenants/{tenant_id}/orders/{order_id}", dependencies=[Depends(require_tenant)])
async def order_detail(tenant_id: str, order_id: str, request: Request):
    db = get_container(request).pdb.for_tenant(tenant_id)
    o = first(await db.table("orders").select("*").eq("id", order_id).limit(1).execute())
    if not o:
        raise HTTPException(404, "not found")
    items = (await db.table("order_items").select("*").eq("order_id", order_id).order("created_at").execute()).data or []
    pays = (await db.table("payments").select("id, provider, reference, status, amount_minor, currency, verified_at, created_at").eq("order_id", order_id).execute()).data or []
    return {"order": o, "items": items, "payments": pays, "contact": (await contacts_by_id(db, [o["contact_id"]])).get(o["contact_id"])}


class OrderStatusIn(BaseModel):
    status: str = Field(pattern="^(PREPARING|READY|COMPLETED|CANCELLED)$")
    notify_customer: bool = True


@router.post("/v1/tenants/{tenant_id}/orders/{order_id}/status", dependencies=[Depends(require_tenant)])
async def set_order_status(tenant_id: str, order_id: str, body: OrderStatusIn, request: Request):
    c = get_container(request)
    db = c.pdb.for_tenant(tenant_id)
    o = first(await db.table("orders").select("*").eq("id", order_id).limit(1).execute())
    if not o:
        raise HTTPException(404, "not found")
    if body.status not in ORDER_FLOW.get(o["status"], set()):
        raise HTTPException(409, f"cannot move an order from {o['status']} to {body.status}")
    won = (await db.table("orders").update({"status": body.status, "updated_at": c.clock().isoformat()}).eq("id", order_id).eq("status", o["status"]).execute()).data
    if not won:
        raise HTTPException(409, "the order changed while you were editing it — refresh and try again")
    await c.events.info("ORDER_STATUS_CHANGED", tenant_id=tenant_id, ref_type="order", ref_id=order_id, from_status=o["status"], to_status=body.status)
    notified = False
    if body.notify_customer and body.status in ("READY", "CANCELLED"):
        contact = first(await db.table("contacts").select("*").eq("id", o["contact_id"]).limit(1).execute())
        cfg = await c.configs.load_active(tenant_id)
        if contact and cfg:
            ctx, _ = build_tenant_runtime(pdb=c.pdb, adapters=c.adapters, events=c.events, llm=c.llm, cfg=cfg, tenant_id=tenant_id, clock=c.clock)
            if body.status == "READY":
                msg = "Your order is ready for pickup! 🎉" if o.get("fulfillment_method") == "PICKUP" else "Your order is ready and on its way to you! 🎉"
            else:
                msg = "We're sorry — we've had to cancel your order. Someone from the team will be in touch about your refund."
            try:
                notified = await ctx.outbox.send(contact["phone"], msg)
            except Exception as e:
                await c.events.warn("ORDER_NOTIFY_FAILED", tenant_id=tenant_id, error=str(e)[:160])
    return {"status": body.status, "customer_notified": notified}


# ═══════════════════════ tenant: menu / availability ═══════════════════════
@router.get("/v1/tenants/{tenant_id}/products", dependencies=[Depends(require_tenant)])
async def list_products(tenant_id: str, request: Request):
    db = get_container(request).pdb.for_tenant(tenant_id)
    products = (await db.table("products").select("*").order("sort_order").execute()).data or []
    groups = (await db.table("modifier_groups").select("*").order("sort_order").execute()).data or []
    mods = (await db.table("modifiers").select("*").order("sort_order").execute()).data or []
    return {"products": [{**p, "groups": [{**g, "modifiers": [m for m in mods if m["group_id"] == g["id"]]} for g in groups if g["product_id"] == p["id"]]} for p in products]}


class AvailabilityIn(BaseModel):
    available: bool


async def _set_available(request: Request, tenant_id: str, table: str, row_id: str, available: bool) -> dict:
    c = get_container(request)
    rows = (await c.pdb.for_tenant(tenant_id).table(table).update({"available": available}).eq("id", row_id).execute()).data
    if not rows:
        raise HTTPException(404, "not found")
    await c.events.info("ITEM_AVAILABILITY_CHANGED", tenant_id=tenant_id, item=rows[0].get("name"), available=available, via="dashboard")
    return {"id": row_id, "available": available}


@router.patch("/v1/tenants/{tenant_id}/products/{product_id}", dependencies=[Depends(require_tenant)])
async def set_product_available(tenant_id: str, product_id: str, body: AvailabilityIn, request: Request):
    return await _set_available(request, tenant_id, "products", product_id, body.available)


@router.patch("/v1/tenants/{tenant_id}/modifiers/{modifier_id}", dependencies=[Depends(require_tenant)])
async def set_modifier_available(tenant_id: str, modifier_id: str, body: AvailabilityIn, request: Request):
    return await _set_available(request, tenant_id, "modifiers", modifier_id, body.available)


@router.get("/v1/tenants/{tenant_id}/integrations", dependencies=[Depends(require_tenant)])
async def list_integrations(tenant_id: str, request: Request):
    c = get_container(request)
    rows = (await c.pdb.for_tenant(tenant_id).table("tenant_integrations").select("id, kind, status, config, created_at").execute()).data or []
    base = c.settings.public_base_url.rstrip("/")
    return {"integrations": [{**r, "payment_webhook_url": f"{base}/v1/webhooks/payments/{tenant_id}/{r['kind']}"} for r in rows]}


# ── media: menu photos and videos ───────────────────────────────────────
@router.post("/v1/tenants/{tenant_id}/media", dependencies=[Depends(require_tenant_admin)])
async def upload_media(tenant_id: str, request: Request, file: UploadFile = File(...)):
    """Upload one photo or MP4 video for this client. Photos are rotated upright, shrunk and stripped of metadata; the file is stored
    in this tenant's own folder and the PUBLIC link is returned for the caller to put on a menu item. WhatsApp fetches that link."""
    c = get_container(request)
    await _tenant_or_404(c, tenant_id)
    if c.media is None:
        raise HTTPException(503, "Media storage isn't set up on this server (it needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY).")
    s = c.settings
    cap = max(s.media_max_image_mb, s.media_max_video_mb) * 1024 * 1024
    data = await file.read(cap + 1)                                   # never buffer more than the largest allowed file
    if len(data) > cap:
        raise HTTPException(413, f"That file is larger than {max(s.media_max_image_mb, s.media_max_video_mb)} MB.")
    try:
        out = process_media(data, max_image_mb=s.media_max_image_mb, max_video_mb=s.media_max_video_mb, max_px=s.media_image_max_px)
    except MediaError as e:
        raise HTTPException(422, str(e))
    path = f"{tenant_id}/{uuid.uuid4().hex}.{out.ext}"
    try:
        url = await c.media.put(path, out.data, out.content_type)
    except MediaStorageError as e:
        await c.events.error("MEDIA_UPLOAD_FAILED", tenant_id=tenant_id, error=str(e)[:200])
        raise HTTPException(502, "Couldn't save the file to storage. Check the Supabase service key and try again.")
    await c.events.info("MEDIA_UPLOADED", tenant_id=tenant_id, kind=out.kind, original_bytes=out.original_bytes, stored_bytes=len(out.data))
    return {"url": url, "type": out.kind, "bytes": len(out.data), "name": (file.filename or "")[:120]}

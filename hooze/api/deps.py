"""Dependency container + auth. The container is built once at startup (or injected in tests)."""
from __future__ import annotations
import functools, hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import Header, HTTPException, Request

from hooze.kernel.db import PlatformDB, first
from hooze.security import Principal, Vault, sha256_hex


@dataclass
class Container:
    settings: Any
    pdb: PlatformDB
    llm: Any
    events: Any
    adapters: Any
    configs: Any
    pipeline: Any
    vault: Optional[Vault]
    payments_for: Callable[[str], Awaitable[Any]]      # tenant_id -> PaymentProvider (raises if not connected)
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    inbox: Any = None            # durable inbound queue (runtime/inbox.py); None => legacy in-memory background task
    maintenance: Any = None      # recovery + monitoring loop
    alerts: Any = None           # AlertManager
    media: Any = None            # media storage (hooze/media/storage.py); None => uploads answer 503

    def extras_for(self, tenant_id: str) -> dict:
        return {"payments": functools.partial(self.payments_for, tenant_id)}


def get_container(request: Request) -> Container:
    return request.app.state.container


async def build_container(settings) -> Container:
    """Production wiring. Kept out of import time so tests never need supabase / google-genai."""
    from hooze.channels.base import AdapterFactory
    from hooze.kernel.db import get_client
    from hooze.kernel.events import EventBus, db_sink, stdout_sink
    from hooze.llm.gateway import build_default_gateway
    from hooze.payments.paystack import PaystackProvider
    from hooze.runtime.pipeline import ConfigLoader, Pipeline

    pdb = PlatformDB(await get_client())
    events = EventBus([stdout_sink, db_sink(pdb)])
    vault = Vault(settings.vault_master_key)

    async def usage(tenant_id: str, kind: str, qty: float, meta: dict) -> None:
        await pdb.table("usage_events").insert({"tenant_id": tenant_id, "kind": kind, "quantity": qty, "metadata": meta}).execute()

    llm = build_default_gateway(settings, usage=usage, events=events)
    adapters, configs = AdapterFactory(vault), ConfigLoader(pdb)

    async def payments_for(tenant_id: str):
        row = first(await pdb.table("tenant_integrations").select("*").eq("tenant_id", tenant_id).eq("kind", "paystack").eq("status", "active").limit(1).execute())
        if not row:
            raise LookupError("no payment provider connected")
        return PaystackProvider(vault.decrypt(row["credentials_enc"]), (row.get("config") or {}).get("email_domain", "hooze-orders.com"))

    c = Container(settings=settings, pdb=pdb, llm=llm, events=events, adapters=adapters, configs=configs, pipeline=None, vault=vault, payments_for=payments_for)
    from hooze.llm.transcription import GroqWhisper
    transcriber = GroqWhisper(settings.groq_api_key) if settings.groq_api_key else None
    if settings.supabase_url and settings.supabase_service_role_key:
        from hooze.media.storage import SupabaseMediaStorage
        c.media = SupabaseMediaStorage(settings.supabase_url, settings.supabase_service_role_key, settings.media_bucket, settings.media_max_video_mb)
    c.pipeline = Pipeline(platform_db=pdb, llm=llm, events=events, adapters=adapters, configs=configs, settings=settings, extras_for=c.extras_for, transcriber=transcriber)

    from hooze.alerts import AlertManager, build_sinks
    from hooze.runtime.inbox import Inbox
    from hooze.runtime.maintenance import Maintenance
    names: dict[str, str] = {}

    async def name_of(tid: str) -> str:
        if tid not in names:
            row = first(await pdb.table("tenants").select("name").eq("id", tid).limit(1).execute())
            names[tid] = (row or {}).get("name") or tid[:8]
        return names[tid]
    c.alerts = AlertManager(build_sinks(settings, pdb, adapters), c.clock, name_of=name_of, dedupe_window_s=settings.alert_dedupe_minutes * 60)
    c.alerts.bus = events
    events.sinks.append(c.alerts)
    c.inbox = Inbox(pdb, c.pipeline, adapters, events, settings, c.clock)
    c.maintenance = Maintenance(c.inbox, pdb, adapters, events, settings, c.clock)
    return c


# ── auth ────────────────────────────────────────────────────────────────
async def _principal(request: Request, x_api_key: Optional[str]) -> Principal:
    c = get_container(request)
    if not x_api_key:
        raise HTTPException(401, "missing API key")
    platform = c.settings.platform_admin_key
    if platform and hmac.compare_digest(x_api_key, platform):        # constant-time (audit M13)
        return Principal(kind="platform")
    row = first(await c.pdb.table("api_keys").select("*").eq("key_hash", sha256_hex(x_api_key)).limit(1).execute())
    if not row or row.get("revoked_at"):
        raise HTTPException(401, "invalid API key")
    role = row.get("role") or "staff"                       # unknown/missing => least privilege that can still work
    if role not in ("admin", "staff", "viewer"):
        role = "viewer"
    return Principal(kind="tenant", tenant_id=row["tenant_id"], key_id=row["id"], role=role)


async def require_platform(request: Request, x_api_key: Optional[str] = Header(None)) -> Principal:
    p = await _principal(request, x_api_key)
    if p.kind != "platform":
        raise HTTPException(403, "platform key required")
    return p


async def require_tenant(tenant_id: str, request: Request, x_api_key: Optional[str] = Header(None)) -> Principal:
    p = await _principal(request, x_api_key)
    if p.kind == "tenant" and p.tenant_id != tenant_id:
        raise HTTPException(403, "key does not belong to this tenant")   # tenant A can never address tenant B's path
    if p.kind == "tenant" and p.role == "viewer" and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(403, "this key is read-only")
    return p


async def require_tenant_admin(tenant_id: str, request: Request, x_api_key: Optional[str] = Header(None)) -> Principal:
    """Configuration, payment keys and other settings that change how the business runs: platform key or a tenant ADMIN key only."""
    p = await require_tenant(tenant_id, request, x_api_key)
    if p.kind == "tenant" and p.role != "admin":
        raise HTTPException(403, "this key is not allowed to change settings (admin key required)")
    return p

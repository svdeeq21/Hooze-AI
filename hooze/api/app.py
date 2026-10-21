from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from hooze.api import admin, internal, operator, webhooks
from hooze.api.deps import Container, build_container
from hooze.settings import get_settings


def create_app(container: Optional[Container] = None) -> FastAPI:
    """Pass a container in tests; in production it is built on startup from settings."""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = get_settings()
        if s.is_production:
            s.assert_production_ready()        # fail at boot with a clear message, not on first request
        app.state.container = await build_container(s)
        c = app.state.container
        task = asyncio.create_task(c.maintenance.loop()) if (s.maintenance_enabled and c.maintenance) else None
        if not c.alerts.sinks:
            await c.events.warn("ALERTS_NOT_CONFIGURED", note="set ALERT_WHATSAPP_NUMBERS and/or ALERT_EMAIL_TO + RESEND_API_KEY")
        try:
            yield
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="Hooze Platform", version="0.1.0", lifespan=None if container else lifespan, docs_url=None if get_settings().is_production else "/docs")
    if container:
        app.state.container = container
    origins = [o.strip().rstrip("/") for o in (container.settings if container else get_settings()).cors_allowed_origins.split(",") if o.strip()]
    if origins:     # an ALLOW-LIST, never "*" (audit M12). Only the dashboard's own origin(s) may call the API from a browser.
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"], max_age=600,
                           allow_headers=["x-api-key", "content-type", "ngrok-skip-browser-warning"])
    for r in (webhooks.router, operator.router, admin.router, internal.router):     # operator BEFORE admin: literal paths beat `{action}`
        app.include_router(r)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        """For an EXTERNAL uptime monitor (the app cannot alert you that it is down): 200 only if the database answers and the queue is sane."""
        c = app.state.container
        try:
            await c.pdb.table("tenants").select("id").limit(1).execute()
            q = await c.inbox.counts() if c.inbox else {}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok", "queue": q}

    return app
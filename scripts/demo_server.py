"""Run the REAL API on seeded, in-memory demo data. No Supabase, Evolution, Paystack or LLM keys needed.

    python -m scripts.demo_server          # http://127.0.0.1:8000   operator key: demo-key

Purpose: click through the dashboard (and develop it) safely. Everything resets when you stop it. Sends go to a recording fake, so
"reply as human" / "mark ready" appear in the history but nothing is sent anywhere. NEVER point production traffic at this.
"""
import asyncio
import os

import uvicorn

from hooze.api.app import create_app
from hooze.api.deps import Container
from hooze.media.storage import MemoryMediaStorage
from hooze.alerts import AlertManager, Sink
from hooze.runtime.inbox import Inbox
from hooze.runtime.maintenance import Maintenance
from hooze.security import Vault
from hooze.settings import Settings
from tests.conftest import food_config, make_world, realestate_config
from tests.fakes import Clock, FakeDB
from tests.test_api import HybridFactory
from tests.test_capabilities import _request
from tests.test_ordering import _pending

class ConsoleSink(Sink):
    """Demo alert destination: prints instead of messaging anyone."""
    name = "console"
    def describe(self): return "Console (demo)"
    async def send(self, title, body): print(f"\n  [ALERT] {body}\n", flush=True)


DEMO_KEY = os.environ.get("DEMO_KEY", "demo-key")
PORT = int(os.environ.get("DEMO_PORT", "8000"))


async def build() -> Container:
    clock, db = Clock(), FakeDB(Clock())
    db.clock = clock
    re_w = await make_world(realestate_config(), tenant_name="praise-dynasty", db=db, clock=clock)
    food_w = await make_world(food_config(), tenant_name="spud-box", db=db, clock=clock)
    await _request(re_w)                      # a pending inspection booking
    await _pending(food_w)                    # an order awaiting payment
    ref = db.rows("payments")[0]["reference"]
    await food_w.cap("ordering").confirm_payment(ref, {"status": "success", "amount_minor": 9_000_00})   # ...which then gets paid
    for sev, ev, tid, msg in [("WARNING", "LLM_PROVIDER_FAILED", re_w.tid, "gemini 503"), ("INFO", "TURN_COMPLETED", re_w.tid, ""),
                              ("ERROR", "PAYMENT_UNKNOWN_REFERENCE", food_w.tid, "ref not found")]:
        await re_w.pdb.table("events").insert({"severity": sev, "event": ev, "tenant_id": tid, "metadata": {"note": msg}}).execute()

    # a message the queue gave up on, so the dashboard's Retry button has something to show (its payload is a non-message event, so a retry completes)
    await re_w.pdb.table("inbound_events").insert({"tenant_id": re_w.tid, "channel_id": re_w.channel["id"], "provider_message_id": "DEMO-FAILED", "payload": {"event": "connection.update"},
                                                   "status": "failed", "attempts": 5, "last_error": "boom: database timeout", "finished_at": clock().isoformat()}).execute()

    async def payments_for(tid): return food_w.payments
    settings = Settings(_env_file=None, app_env="development", platform_admin_key=DEMO_KEY, internal_cron_secret="demo-cron", public_base_url=f"http://127.0.0.1:{PORT}",
                        cors_allowed_origins="http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173")
    adapters = HybridFactory(re_w.adapter)
    inbox = Inbox(re_w.pdb, re_w.pipeline, adapters, re_w.events, settings, clock)
    alerts = AlertManager([ConsoleSink()], clock, inline=True)
    alerts.bus = re_w.events
    return Container(settings=settings, pdb=re_w.pdb, llm=re_w.llm, events=re_w.events, adapters=adapters, configs=re_w.configs, pipeline=re_w.pipeline,
                     vault=Vault(Vault.generate_key()), payments_for=payments_for, clock=clock, inbox=inbox, alerts=alerts, maintenance=Maintenance(inbox, re_w.pdb, adapters, re_w.events, settings, clock),
                     media=MemoryMediaStorage(base=f"http://127.0.0.1:{PORT}/demo-media"))      # demo only: served from memory by the route in make_app()


async def make_app():
    """The demo app. Its in-memory storage hands out links, so this serves those bytes back; otherwise an uploaded photo's link is dead."""
    from fastapi import HTTPException, Response
    container = await build()
    app = create_app(container)

    @app.get("/demo-media/{path:path}")
    async def demo_media(path: str):
        stored = container.media.files.get(path)
        if stored is None:
            raise HTTPException(404, "not found")
        data, content_type = stored
        return Response(content=data, media_type=content_type, headers={"cache-control": "no-store"})
    return app


async def main():
    app = await make_app()
    print(f"\n  DEMO API  http://127.0.0.1:{PORT}   operator key: {DEMO_KEY}\n", flush=True)
    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")).serve()


if __name__ == "__main__":
    asyncio.run(main())

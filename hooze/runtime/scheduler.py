"""Background work for ALL tenants, driven by one external cron hitting POST /internal/tick.

Same reasoning as pd `internal.py`: NOT an in-process loop (the host can sleep/restart and silently drop cycles);
an HTTP call from Render Cron / GitHub Actions / Railway shows up in the logs like any other request.
One tenant's failure never stops the others.
"""
from __future__ import annotations
from hooze.kernel.locks import try_lock
from hooze.runtime.tenant_runtime import build_tenant_runtime


async def run_tick(*, pdb, configs, adapters, events, llm, clock, extras_for) -> dict:
    """One sweep for all tenants. Overlapping sweeps (slow tenant + a cron that fires again) are refused, not queued."""
    async with try_lock("internal-tick") as lock:
        if not lock.acquired:
            await events.warn("TICK_SKIPPED_OVERLAP")
            return {"status": "already_running", "tenants": 0, "results": {}}
        return await _sweep(pdb=pdb, configs=configs, adapters=adapters, events=events, llm=llm, clock=clock, extras_for=extras_for)


async def _sweep(*, pdb, configs, adapters, events, llm, clock, extras_for) -> dict:
    tenants = (await pdb.table("tenants").select("*").eq("status", "active").execute()).data or []
    out: dict = {}
    for t in tenants:
        tid = t["id"]
        try:
            cfg = await configs.load_active(tid)
            if cfg is None:
                continue
            _, caps = build_tenant_runtime(pdb=pdb, adapters=adapters, events=events, llm=llm, cfg=cfg, tenant_id=tid, clock=clock, extras=extras_for(tid))
            out[tid] = {name: await cap.tick() for name, cap in caps.items()}
        except Exception as e:
            await events.error("TICK_FAILED", tenant_id=tid, error=str(e)[:200])
            out[tid] = {"error": str(e)[:200]}
    return {"status": "ok", "tenants": len(tenants), "results": out}

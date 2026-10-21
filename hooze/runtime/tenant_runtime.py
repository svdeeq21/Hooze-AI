"""One way to build a tenant's capability set, used by the pipeline, cron ticks and the API."""
from __future__ import annotations
from hooze.capabilities.base import CapabilityContext, build_capabilities
from hooze.runtime.outbox import Outbox


def build_tenant_runtime(*, pdb, adapters, events, llm, cfg, tenant_id: str, clock, extras: dict | None = None):
    outbox = Outbox(pdb, adapters, tenant_id, cfg.staff_numbers, events, clock=clock)
    ctx = CapabilityContext(tenant_id=tenant_id, db=pdb.for_tenant(tenant_id), events=events, llm=llm, outbox=outbox,
                            config=cfg, clock=clock, extras=extras or {})
    enabled = {"handoff": {}, **cfg.capabilities}      # every tenant ALWAYS has a human escape hatch
    return ctx, build_capabilities(ctx, enabled)

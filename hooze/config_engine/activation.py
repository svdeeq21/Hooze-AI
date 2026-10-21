"""Activation: validate -> compile -> seed -> flip live. Order matters: seeds are written BEFORE the config row
goes `active`, and ANY failed seed (including soft failures that are counted rather than raised) aborts the flip,
so the previous config keeps serving customers — never a half-built tenant."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from hooze.capabilities.knowledge import sync_documents
from hooze.config_engine.compiler import compile_config
from hooze.config_engine.schema import BusinessConfig
from hooze.config_engine.validator import ValidationReport, validate
from hooze.kernel.db import first


@dataclass
class ActivationResult:
    ok: bool
    report: ValidationReport
    version: Optional[int] = None
    seeded: dict = field(default_factory=dict)
    error: str = ""
    retryable: bool = False      # True => a dependency (e.g. the embedding provider) failed; the config itself is fine, try again


async def _write(db, op: str, fields: dict, present: dict, row_id=None):
    """Write a product with its presentation fields (category, media). If migration 0005 has not been applied the extra columns
    do not exist: write the rest anyway (activation must not fail over decoration) — the menu just has no sections/photos yet."""
    def build(data):
        t = db.table("products")
        return t.update(data).eq("id", row_id) if op == "update" else t.insert(data)
    try:
        return await build({**fields, **present}).execute()
    except Exception as e:
        if any(k in str(e).lower() for k in ("category", "media")):       # missing column(s): save the product without them
            return await build(fields).execute()
        raise


async def _seed_catalog(db, catalog: list[dict]) -> dict:
    existing = {p["name"]: p for p in ((await db.table("products").select("*").execute()).data or [])}
    n = 0
    for i, p in enumerate(catalog):
        row = existing.get(p["name"])
        fields = {"name": p["name"], "description": p["description"], "price_minor": p["price_minor"], "sort_order": i}
        present = {"category": p.get("category") or "", "media": p.get("media") or []}      # needs migration 0005
        if row:
            await _write(db, "update", fields, present, row["id"])
            continue                                    # existing product: keep its options (owner may have edited availability)
        prod = (await _write(db, "insert", {**fields, "available": True}, present)).data[0]
        for gi, g in enumerate(p["groups"]):
            grp = (await db.table("modifier_groups").insert({"product_id": prod["id"], "name": g["name"], "selection": g["selection"], "required": g["required"],
                                                             "max_selections": g["max_selections"], "sort_order": gi}).execute()).data[0]
            for oi, o in enumerate(g["options"]):
                await db.table("modifiers").insert({"group_id": grp["id"], "name": o["name"], "price_minor": o["price_minor"], "available": True, "sort_order": oi}).execute()
        n += 1
    return {"products_created": n}


async def activate(pdb, llm, tenant_id: str, bc: BusinessConfig, *, configs=None, activated_by: Optional[str] = None) -> ActivationResult:
    report = validate(bc)
    if not report.ok:
        return ActivationResult(False, report)
    db = pdb.for_tenant(tenant_id)
    prev = (await db.table("tenant_configs").select("version").order("version", desc=True).limit(1).execute()).data or []
    version = (prev[0]["version"] + 1) if prev else 1
    compiled = compile_config(bc, version)

    seeded: dict = {}
    if compiled.knowledge_docs:
        async def embedder(t: str): return await llm.embed(tenant_id, t)
        seeded["knowledge"] = await sync_documents(db, embedder, compiled.knowledge_docs, source="config")
        if seeded["knowledge"]["failed"]:
            # Never flip a tenant live with a partly-empty knowledge base. Seeds already written are harmless (no active config
            # points at them yet) and a retry is cheap: unchanged docs are skipped by the content hash.
            return ActivationResult(False, report, seeded=seeded, retryable=True,
                                    error=f"{seeded['knowledge']['failed']} of {len(compiled.knowledge_docs)} knowledge items could not be embedded; not activated")
    if compiled.catalog:
        seeded["catalog"] = await _seed_catalog(db, compiled.catalog)

    # ONE transaction: archive the old active config AND insert the new one (and stamp activated_at). Previously these were two
    # statements, so a failure between them left the tenant with NO active config and the bot silently ignoring every message.
    await db.rpc("activate_config_tx", {"p_version": version, "p_config": bc.model_dump(mode="json"),
                                        "p_compiled": compiled.runtime.model_dump(mode="json", by_alias=True),
                                        "p_report": report.to_dict(), "p_created_by": activated_by}).execute()
    if configs:
        configs.invalidate(tenant_id)
    return ActivationResult(True, report, version, seeded)

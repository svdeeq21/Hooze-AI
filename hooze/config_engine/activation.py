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


def _differs(row: dict, fields: dict) -> bool:
    return any(row.get(k) != v for k, v in fields.items())


async def _sync_options(db, product_id: str, groups_cfg: list[dict], groups: list[dict], mods_by_group: dict) -> dict:
    """Make a product's option groups and options equal to the config, matched by NAME. Prices, ordering and rules are updated in place;
    the owner's per-option `available` (sold-out) flag is kept; options/groups no longer in the config are deleted."""
    stats = {"options_added": 0, "options_updated": 0, "options_removed": 0}
    old_groups = {g["name"]: g for g in groups if g["product_id"] == product_id}
    for gi, g in enumerate(groups_cfg):
        gfields = {"selection": g["selection"], "required": g["required"], "max_selections": g["max_selections"], "sort_order": gi}
        grp = old_groups.pop(g["name"], None)
        if grp is None:
            grp = (await db.table("modifier_groups").insert({"product_id": product_id, "name": g["name"], **gfields}).execute()).data[0]
        elif _differs(grp, gfields):
            await db.table("modifier_groups").update(gfields).eq("id", grp["id"]).execute()
        old_mods = {m["name"]: m for m in mods_by_group.get(grp["id"], [])}
        for oi, o in enumerate(g["options"]):
            ofields = {"price_minor": o["price_minor"], "sort_order": oi}
            m = old_mods.pop(o["name"], None)
            if m is None:
                await db.table("modifiers").insert({"group_id": grp["id"], "name": o["name"], "available": True, **ofields}).execute()
                stats["options_added"] += 1
            elif _differs(m, ofields):
                await db.table("modifiers").update(ofields).eq("id", m["id"]).execute()          # NOT touching `available`
                stats["options_updated"] += 1
        for m in old_mods.values():
            await db.table("modifiers").delete().eq("id", m["id"]).execute()
            stats["options_removed"] += 1
    for grp in old_groups.values():                                                              # whole group dropped from the config
        for m in mods_by_group.get(grp["id"], []):
            await db.table("modifiers").delete().eq("id", m["id"]).execute()
            stats["options_removed"] += 1
        await db.table("modifier_groups").delete().eq("id", grp["id"]).execute()
    return stats


async def _seed_catalog(db, catalog: list[dict]) -> dict:
    """Make the live menu (products table) match the config, matched by product NAME:
      * new product            -> created (available);
      * existing product       -> name/description/price/category/photos/order AND its options are updated in place; the owner's
                                  sold-out toggles on the product and on each option are kept;
      * product not in config  -> DELETED, so it can no longer be ordered or shown. Safe for history: order lines snapshot name, price
                                  and options and do not point at the products table.
    An EMPTY catalog changes nothing (callers skip this), so a config that failed to produce items can never wipe a live menu.
    Note: today the config is the source of truth for the menu. If a bulk import ever manages products directly, it must not also be
    pruned by this function."""
    if not catalog:
        return {"products_created": 0, "products_updated": 0, "products_removed": 0, "options_added": 0, "options_updated": 0, "options_removed": 0}
    existing_rows = (await db.table("products").select("*").execute()).data or []
    groups = (await db.table("modifier_groups").select("*").execute()).data or []
    mods_by_group: dict = {}
    for m in (await db.table("modifiers").select("*").execute()).data or []:
        mods_by_group.setdefault(m["group_id"], []).append(m)
    existing: dict = {}
    stale: list[dict] = []                      # rows to delete: not in the config, or a second row carrying the same name
    for r in sorted(existing_rows, key=lambda r: str(r.get("created_at") or "")):
        if r["name"] in existing:
            stale.append(r)
        else:
            existing[r["name"]] = r

    stats = {"products_created": 0, "products_updated": 0, "products_removed": 0, "options_added": 0, "options_updated": 0, "options_removed": 0}
    for i, p in enumerate(catalog):
        fields = {"name": p["name"], "description": p["description"], "price_minor": p["price_minor"], "sort_order": i}
        present = {"category": p.get("category") or "", "media": p.get("media") or []}      # needs migration 0005
        row = existing.pop(p["name"], None)
        if row:
            if _differs({**{"category": "", "media": []}, **row}, {**fields, **present}):
                await _write(db, "update", fields, present, row["id"])
                stats["products_updated"] += 1
            product_id = row["id"]
        else:
            product_id = (await _write(db, "insert", {**fields, "available": True}, present)).data[0]["id"]
            stats["products_created"] += 1
        for k, v in (await _sync_options(db, product_id, p["groups"], groups, mods_by_group)).items():
            stats[k] += v

    for row in [*existing.values(), *stale]:                                                 # what is left was not in the config
        for g in [g for g in groups if g["product_id"] == row["id"]]:
            for m in mods_by_group.get(g["id"], []):
                await db.table("modifiers").delete().eq("id", m["id"]).execute()
            await db.table("modifier_groups").delete().eq("id", g["id"]).execute()
        await db.table("products").delete().eq("id", row["id"]).execute()
        stats["products_removed"] += 1
    return stats


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
        async def embedder(t: str): return await llm.embed(tenant_id, t, kind="document")
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

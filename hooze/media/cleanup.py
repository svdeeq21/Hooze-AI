"""Housekeeping for uploaded menu photos/videos.

Files live in the tenant's own folder (`<tenant_id>/<random>.<ext>`); the database only stores their URLs. Nothing ever deleted a file, so
removed menu items and replaced photos left bytes behind forever. After each save we delete the files nothing live points at.

SAFETY (this deletes customer-uploaded files, so it is deliberately cautious):
  * only ever touches `<tenant_id>/…` for a valid tenant UUID;
  * a file counts as IN USE if its name appears anywhere in a live product, the active config, or any knowledge item;
  * files younger than `min_age_hours` are never deleted (an upload may not have been placed on an item / saved yet);
  * if there is nothing live to compare against (no active config) it refuses, so a failed or empty read can never wipe a folder;
  * a cap per run; and a dry run that only reports.
"""
from __future__ import annotations
import json, uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _valid_tenant(tenant_id: str) -> str:
    return str(uuid.UUID(tenant_id))                      # raises ValueError for anything that is not a UUID: never build a path from it


async def live_references(db) -> Optional[str]:
    """One big string holding everything that can point at an uploaded file, or None if there is no active config to trust."""
    cfg = (await db.table("tenant_configs").select("*").eq("status", "active").execute()).data or []
    if not cfg:
        return None
    products = (await db.table("products").select("media").execute()).data or []
    knowledge = (await db.table("knowledge_items").select("media").execute()).data or []
    return json.dumps([cfg, products, knowledge], default=str)


async def sweep_tenant(pdb, storage, tenant_id: str, *, now: datetime, min_age_hours: int = 24, dry_run: bool = False, max_delete: int = 200) -> dict:
    tid = _valid_tenant(tenant_id)
    files = await storage.list_files(tid)
    out = {"listed": len(files), "in_use": 0, "too_new": 0, "deleted": 0, "would_delete": [], "skipped": ""}
    if not files:
        return out
    blob = await live_references(pdb.for_tenant(tid))
    if blob is None:
        out["skipped"] = "no active configuration to compare against"
        return out
    cutoff = now - timedelta(hours=min_age_hours)
    orphans = []
    for f in files:
        name = f["path"].rsplit("/", 1)[-1]
        if not f["path"].startswith(tid + "/"):
            continue
        if name in blob:
            out["in_use"] += 1
            continue
        made = _parse(f.get("created_at"))
        if made is None or made > cutoff:                 # unknown age counts as young
            out["too_new"] += 1
            continue
        orphans.append(f["path"])
    orphans = orphans[:max_delete]
    out["would_delete"] = orphans
    if not dry_run and orphans:
        out["deleted"] = await storage.delete(orphans)
    return out


async def purge_tenant(storage, tenant_id: str) -> int:
    """Delete EVERY file in the tenant's folder (the client is being removed)."""
    tid = _valid_tenant(tenant_id)
    paths = [f["path"] for f in await storage.list_files(tid) if f["path"].startswith(tid + "/")]
    return await storage.delete(paths)


async def sweep_after_save(c, tenant_id: str) -> None:
    """Run in the background after a successful save. Never raises, never blocks the save: a failure is only logged."""
    s = c.settings
    if c.media is None or not getattr(s, "media_cleanup_enabled", True):
        return
    try:
        r = await sweep_tenant(c.pdb, c.media, tenant_id, now=c.clock(), min_age_hours=s.media_orphan_min_age_hours)
        if r["deleted"] or r["skipped"]:
            await c.events.info("MEDIA_CLEANUP", tenant_id=tenant_id, deleted=r["deleted"], in_use=r["in_use"], too_new=r["too_new"], skipped=r["skipped"])
    except Exception as e:
        await c.events.warn("MEDIA_CLEANUP_FAILED", tenant_id=tenant_id, error=str(e)[:200])

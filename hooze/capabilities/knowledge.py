"""Knowledge / RAG over tenant-owned records (listings, services, FAQs, policies).

Extracted from: pd `rag.py` (embed + match RPC + threshold fallback), pd `inventory.py`,
pd `firebase_sync.py` (content-hash diff so unchanged records aren't re-embedded),
pd `property_delivery.py` / `whatsapp.send_property_media_bundle` (verified-record delivery).

Three design decisions, all from pd's own postmortems:
  1. VERIFIED-RECORD DELIVERY. The LLM never writes a price, link or spec. It chooses a
     record (`share_item ref=K2`); the backend renders the card from the record and sends
     its media. pd's hallucinated-listing bugs ("Never announce a property title, price..."
     in the prompt) become structurally impossible instead of prompt-policed.
  2. SHORT REFS, NOT UUIDs, in prompts (K1, K2). The model can't hallucinate or leak an id.
  3. BOUNDED-CONCURRENCY SYNC (audit H9: pd embedded sequentially inside the live process,
     freezing the bot during a sync). And no arbitrary `LIMIT 20` before scoring (H8):
     ranking happens in the `match_knowledge` RPC over the whole tenant corpus.
"""
from __future__ import annotations
from datetime import datetime
import asyncio, hashlib, json, re
from dataclasses import dataclass, field
from typing import Any, Optional
from pydantic import BaseModel

from hooze.capabilities.base import Capability, register
from hooze.kernel.db import first
from hooze.runtime import guards
from hooze.runtime.actions import Tool
from hooze.runtime.models import ToolResult

CLAIM_PATTERNS = [r"\b(sending|sent|attached|attaching)\b.*\b(details|listing|photos?|pictures?|video|brochure|link)\b",
                  r"\bhere (is|are)\b.*\b(details|listing|photos?|link)\b"]


EMBED_RECIPE = "document-embeddings-v2"      # v1 = documents were embedded as search queries


@dataclass
class KnowledgeDoc:
    external_id: str
    title: str
    content: str
    kind: str = "general"
    data: dict = field(default_factory=dict)
    media: list = field(default_factory=list)        # [{"type": "image"|"video", "url": "..."}]
    is_active: bool = True

    def content_hash(self) -> str:
        # EMBED_RECIPE is part of the hash on purpose: changing how documents are embedded makes every stored vector stale, and a hash that
        # ignored it would skip them as "unchanged" forever. Bump it and the next activation re-embeds everything once.
        return hashlib.sha256(json.dumps([EMBED_RECIPE, self.title, self.content, self.kind, self.data, self.media, self.is_active], sort_keys=True, default=str).encode()).hexdigest()


async def sync_documents(db, embedder, docs: list[KnowledgeDoc], *, source: str, concurrency: int = 4) -> dict:
    """Idempotent hash-diff sync. Only new/changed docs are embedded; docs missing from the feed are deactivated."""
    existing = {r["external_id"]: r for r in ((await db.table("knowledge_items").select("*").eq("source", source).execute()).data or [])}
    sem, stats = asyncio.Semaphore(concurrency), {"created": 0, "updated": 0, "unchanged": 0, "deactivated": 0, "failed": 0}

    async def one(d: KnowledgeDoc):
        h, row = d.content_hash(), existing.get(d.external_id)
        if row and row.get("content_hash") == h:
            stats["unchanged"] += 1
            return
        async with sem:
            try:
                emb = await embedder(f"{d.title}\n{d.content}")
            except Exception:
                stats["failed"] += 1
                return
        payload = {"kind": d.kind, "title": d.title, "content": d.content, "data": d.data, "media": d.media, "is_active": d.is_active,
                   "content_hash": h, "embedding": emb, "source": source, "external_id": d.external_id}
        if row:
            await db.table("knowledge_items").update(payload).eq("id", row["id"]).execute(); stats["updated"] += 1
        else:
            await db.table("knowledge_items").insert(payload).execute(); stats["created"] += 1

    await asyncio.gather(*(one(d) for d in docs))
    seen = {d.external_id for d in docs}
    for ext, row in existing.items():
        if ext not in seen and row.get("is_active"):
            await db.table("knowledge_items").update({"is_active": False}).eq("id", row["id"]).execute(); stats["deactivated"] += 1
    return stats


class _Share(BaseModel):
    ref: str


def render_card(item: dict, fields: list[dict]) -> str:
    data = {"title": item.get("title", ""), **(item.get("data") or {})}
    lines = []
    for f in fields:
        v = data.get(f["field"])
        if v not in (None, "", []):
            lines.append(f.get("fmt", "{v}").format(v=", ".join(map(str, v)) if isinstance(v, list) else v))
    if len(lines) <= 1 and (item.get("content") or "").strip():
        # Only the title rendered (an FAQ or a record with no card fields): sending just the title posted a bare QUESTION to the customer
        # ("How do I get started?") with no answer. Include the record's text, trimmed.
        lines.append(item["content"].strip()[:700])
    return "\n".join(lines) or item.get("title", "")


@register
class KnowledgeCapability(Capability):
    name = "knowledge"

    async def context(self, turn) -> str:
        try:
            emb = await turn.llm.embed(turn.tenant_id, turn.text, fail_fast=True)
            params = {"p_embedding": emb, "p_threshold": self.config.get("threshold", 0.30), "p_count": self.config.get("top_k", 5), "p_kind": None}
            items = (await turn.db.rpc("match_knowledge", params).execute()).data or []
        except Exception as e:
            await turn.events.warn("KNOWLEDGE_LOOKUP_FAILED", tenant_id=turn.tenant_id, trace_id=turn.trace_id, error=str(e)[:160])
            return "RELEVANT RECORDS: (lookup unavailable — do not state specifics; offer to have a person follow up)"
        refs = {f"K{i + 1}": it for i, it in enumerate(items)}
        turn.scratch["knowledge_refs"] = refs
        if not refs:
            return "RELEVANT RECORDS: (none matched). Do not invent any; say you'll check, or ask what they're looking for."
        lines = ["RELEVANT RECORDS (verified). State ONLY what is written here. Answer questions in your own words from the record text. Only if the customer asks to SEE a record's full details/photos, propose share_item with its ref (never twice to the same customer)."]
        for ref, it in refs.items():
            lines.append(f"[{ref}] {it.get('title', '')}: {(it.get('content') or '')[:500]}")
        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        async def share_item(turn, args: _Share) -> ToolResult:
            item = (turn.scratch.get("knowledge_refs") or {}).get(args.ref.strip().upper())
            if not item:
                return ToolResult(ok=False, internal_error=f"unknown ref {args.ref}")
            attrs = dict(turn.contact.get("attributes") or {})
            shared = dict(attrs.get("shared_items") or {})
            last, now = shared.get(item["id"]), self.ctx.clock()
            if last and (now - datetime.fromisoformat(last)).total_seconds() < self.config.get("reshare_after_hours", 24) * 3600:
                # The same full card re-sent on every pitch is a brochure, not a conversation (real chat: the product card went to the same
                # prospect twice). Not ok => the model's "here are the details again" claim is stripped and its own short reply stands.
                return ToolResult(ok=False, internal_error=f"card for {item['id']} already shared recently")
            fields = self.config.get("card_fields") or [{"field": "title", "fmt": "*{v}*"}]
            await turn.out.text(render_card(item, fields), meta={"kind": "item_card", "item_id": item["id"]}, typing=False)
            if self.config.get("share_media", True):
                for m in (item.get("media") or [])[: self.config.get("max_media", 4)]:
                    await turn.out.media(m["url"], m.get("type", "image"))
            await turn.events.info("ITEM_SHARED", tenant_id=turn.tenant_id, trace_id=turn.trace_id, ref_type="knowledge_item", ref_id=item["id"])
            shared[item["id"]] = now.isoformat()
            attrs["shared_items"] = shared
            turn.contact["attributes"] = attrs
            try:
                await turn.store.set_contact(turn.contact["id"], attributes=attrs)
            except Exception as e:                       # the card WAS sent; a bookkeeping failure must not fail the tool
                await turn.events.warn("SHARED_ITEM_RECORD_FAILED", tenant_id=turn.tenant_id, error=str(e)[:120])
            return ToolResult(data={"item_id": item["id"], "title": item.get("title")})
        return [Tool("share_item", "send the full card (details + photos) of ONE record from RELEVANT RECORDS. Only when the customer asks to see or get that record; never to introduce the business or answer a general question", _Share, share_item, self.name)]

    def check_reply(self, turn, reply: str, results: list) -> Optional[str]:
        if any(r.tool == "share_item" and r.ok for r in results):
            return None
        if any(re.search(p, reply, re.I) for p in CLAIM_PATTERNS):
            cleaned = guards.strip_sentences(reply, CLAIM_PATTERNS)
            return cleaned or "Which one would you like the full details for?"
        return None

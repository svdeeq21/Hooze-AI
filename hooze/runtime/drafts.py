"""Assist mode: the assistant writes, a person approves.

In `assist` mode every message the assistant would send during a turn (text, cards, photos) is saved here as a DRAFT instead
of going to WhatsApp. The dashboard lists pending drafts per chat; a person sends one as written, edits it first, or discards
it. Everything else in the turn still happens for real (cart updates, booking requests, hand-offs), because those are done by
code, not by the AI's wording. Out-of-turn messages (payment receipts, booking confirmations a person already decided) are not
drafted: a person or a verified payment already approved them.
"""
from __future__ import annotations
from typing import Optional

from hooze.kernel.db import first
from hooze.runtime.store import ConversationStore


class DraftSink:
    def __init__(self, db, conversation_id: str, contact_id: Optional[str], events, tenant_id: str, trace_id: str):
        self.db, self.conversation_id, self.contact_id = db, conversation_id, contact_id
        self.events, self.tenant_id, self.trace_id = events, tenant_id, trace_id
        self.count = 0

    async def text(self, text: str) -> None:
        await self.db.table("reply_drafts").insert({"conversation_id": self.conversation_id, "contact_id": self.contact_id,
                                                    "kind": "text", "content": text, "status": "pending"}).execute()
        self.count += 1

    async def media(self, url: str, media_type: str, caption: str = "") -> None:
        await self.db.table("reply_drafts").insert({"conversation_id": self.conversation_id, "contact_id": self.contact_id, "kind": "media",
                                                    "content": caption, "media_url": url, "media_type": media_type, "status": "pending"}).execute()
        self.count += 1


class DraftError(Exception):
    """Safe to show to the person approving: what went wrong and what to do."""
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def send_draft(*, pdb, adapters, tenant_id: str, draft_id: str, clock, actor: str, text: Optional[str] = None) -> dict:
    """Send one pending draft (optionally edited) from the number the chat is on, then record it in the transcript.

    Exactly-once: the draft is first CLAIMED (pending -> sending) with a conditional update, so two people pressing Send at the same
    moment cannot both send it. If WhatsApp refuses, the draft goes back to pending so it can be retried; once the send succeeded it is
    marked sent even if the bookkeeping afterwards fails (never resend something the customer already has)."""
    db = pdb.for_tenant(tenant_id)
    d = first(await db.table("reply_drafts").select("*").eq("id", draft_id).limit(1).execute())
    if not d:
        raise DraftError(404, "That draft doesn't exist.")
    if d["status"] != "pending":
        raise DraftError(409, f"That draft was already {d['status']}.")
    won = (await db.table("reply_drafts").update({"status": "sending", "decided_by": actor}).eq("id", draft_id).eq("status", "pending").execute()).data
    if not won:
        raise DraftError(409, "Someone else is handling that draft. Refresh to see it.")
    conv = first(await db.table("conversations").select("*").eq("id", d["conversation_id"]).limit(1).execute())
    contact = conv and first(await db.table("contacts").select("*").eq("id", conv["contact_id"]).limit(1).execute())
    ch = conv and first(await pdb.table("channels").select("*").eq("id", conv["channel_id"]).eq("tenant_id", tenant_id).limit(1).execute())
    if not (conv and contact and ch):
        await db.table("reply_drafts").update({"status": "pending", "decided_by": None}).eq("id", draft_id).execute()
        raise DraftError(404, "The chat for that draft no longer exists.")
    body = (text if text is not None else d["content"]).strip()
    adapter = adapters.for_channel(ch)
    try:
        if d["kind"] == "media":
            await adapter.send_media(contact["phone"], d["media_url"], d.get("media_type") or "image", body)
        else:
            if not body:
                raise DraftError(422, "The message is empty.")
            await adapter.send_text(contact["phone"], body, typing_delay=0.0)
    except DraftError:
        await db.table("reply_drafts").update({"status": "pending", "decided_by": None}).eq("id", draft_id).execute()
        raise
    except Exception as e:
        await db.table("reply_drafts").update({"status": "pending", "decided_by": None}).eq("id", draft_id).execute()
        raise DraftError(502, f"WhatsApp didn't accept the message: {str(e)[:160]}. The draft is still waiting; try again.")
    now = clock().isoformat()
    await db.table("reply_drafts").update({"status": "sent", "decided_at": now, "content": body}).eq("id", draft_id).execute()
    edited = text is not None and text.strip() != (d["content"] or "").strip()
    try:
        store = ConversationStore(db, clock)
        content = body if d["kind"] == "text" else f"[{d.get('media_type') or 'image'}] {body}".strip()
        await store.record_outbound(conv["id"], content, "bot", {"approved_by": actor, "edited": edited, **({"media_url": d["media_url"]} if d["kind"] == "media" else {})})
    except Exception:
        pass                                    # sent is sent: a missing transcript row must never cause a resend
    return {"sent": True, "edited": edited}


async def discard_draft(*, pdb, tenant_id: str, draft_id: str, clock, actor: str) -> bool:
    rows = (await pdb.for_tenant(tenant_id).table("reply_drafts").update({"status": "discarded", "decided_by": actor, "decided_at": clock().isoformat()})
            .eq("id", draft_id).eq("status", "pending").execute()).data
    return bool(rows)

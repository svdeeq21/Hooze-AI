"""Human handoff. Mandatory for every tenant.

Extracted from: pd `escalation.py` (escalate = mark HUMAN_REQUIRED + pause AI + system
message + alert), pd `intent_engine.user_requested_human`, pd notifications HUMAN_REQUESTED,
pd `_handle_pause_command` / `_handle_resume_command` (staff `resume <number>`).

Fixes carried in:
  * audit M6: bare "call me" matched "you can call me John". Patterns now require a
    request shape ("call me back", "call me please", or "call me" on its own) and reject
    "you can / just call me <name>".
  * audit M7: the human's name is config (`contact_name`), not hard-coded "Sadiq".
  * every tenant gets this automatically (tenant_runtime.py), because the pipeline's
    failure paths (LLM down, stuck qualification) depend on it.
"""
from __future__ import annotations
import re
from typing import Optional
from pydantic import BaseModel

from hooze.capabilities.base import Capability, GateResult, StaffCommand, register
from hooze.kernel.db import first
from hooze.runtime.actions import Tool
from hooze.runtime.models import ToolResult

DEFAULT_HUMAN_PATTERNS = [
    r"\b(speak|talk|chat)\s+(to|with)\s+(a\s+|an\s+|the\s+)?(human|person|agent|representative|manager|someone|somebody|real person)\b",
    r"\b(need|want|get me|give me)\s+(a\s+|an\s+)?(human|real person|agent|representative)\b",
    r"(?<!you can )(?<!just )\bcall\s+me\s+(back|now|please|asap|today|later|tomorrow)\b",
    r"(?<!you can )(?<!just )\bcall\s+me\s*[.!?]*$",
    r"\bgive\s+me\s+a\s+call\b",
]



async def set_ai_paused(db, contact_id: str, paused: bool, *, conversation_id: Optional[str] = None) -> None:
    """Pause/resume the assistant for a contact. Two rows hold this state (`contacts.ai_paused` and `conversations.status`); they cannot be
    one transaction here. The pipeline treats a customer as paused if EITHER says so, so a half-done change is never a customer who is
    "resumed" but unanswered by accident: it is simply still paused. What goes wrong is the HUMAN being misled, so:
      * the writes are ordered so the contact flag (what the dashboard shows as "AI paused") only turns off LAST on resume and on FIRST
        on pause, i.e. after a failure the dashboard never shows "resumed" for a chat that is still silenced;
      * the caller must treat an exception as "not done" and say so (both callers do); repeating the call is safe (idempotent)."""
    status = {"status": "handoff" if paused else "active"}
    def conv():
        q = db.table("conversations").update(status)
        return q.eq("id", conversation_id) if conversation_id else q.eq("contact_id", contact_id)
    if paused:
        await db.table("contacts").update({"ai_paused": True}).eq("id", contact_id).execute()
        await conv().execute()
    else:
        await conv().execute()
        await db.table("contacts").update({"ai_paused": False}).eq("id", contact_id).execute()


class _NoArgs(BaseModel):
    reason: str = ""


@register
class HandoffCapability(Capability):
    name = "handoff"

    def _who(self) -> str:
        return self.config.get("contact_name") or "a member of our team"

    def _patterns(self) -> list[str]:
        return DEFAULT_HUMAN_PATTERNS + list(self.config.get("human_request_patterns", []))

    async def escalate(self, turn, reason: str, **meta) -> None:
        """Pause the AI for this contact, mark the conversation, tell the humans. Idempotent."""
        if turn.contact.get("ai_paused"):
            return
        await turn.store.set_contact(turn.contact["id"], ai_paused=True)
        turn.contact["ai_paused"] = True
        await turn.store.set_conversation(turn.conversation["id"], status="handoff", handoff_reason=reason)
        turn.conversation["status"] = "handoff"
        await turn.db.table("messages").insert({
            "conversation_id": turn.conversation["id"], "direction": "outbound", "sender_type": "system",
            "content": f"Escalated to a human: {reason}", "content_type": "system", "meta": meta}).execute()
        await turn.events.warn("ESCALATED", tenant_id=turn.tenant_id, trace_id=turn.trace_id, ref_type="conversation",
                               ref_id=turn.conversation["id"], reason=reason, **meta)
        name = turn.contact.get("display_name") or "Unknown"
        slots = (turn.contact.get("attributes") or {}).get("slots") or {}
        detail = ", ".join(f"{k}={v}" for k, v in slots.items()) or "none captured yet"
        await self.ctx.outbox.notify_staff(
            f"🔔 Human needed ({reason})\n{name} — +{turn.contact['phone']}\nStage: {turn.state or turn.conversation.get('state')}\n"
            f"Known: {detail}\nReply RESUME {turn.contact['phone']} to hand the chat back to the assistant.")

    async def gate(self, turn) -> Optional[GateResult]:
        if any(re.search(p, turn.text, re.I) for p in self._patterns()):
            await self.escalate(turn, "HUMAN_REQUESTED")
            return GateResult(reply=f"Of course — I'm bringing in {self._who()} now. They'll message you shortly.")
        return None

    def tools(self) -> list[Tool]:
        async def request_human(turn, args: _NoArgs) -> ToolResult:
            await self.escalate(turn, args.reason or "HUMAN_REQUESTED_BY_MODEL")
            return ToolResult(message=f"Understood — I'm bringing in {self._who()} to help you directly. They'll message you shortly.")
        return [Tool("request_human", "hand the conversation to a person (customer asked, or is upset)", _NoArgs, request_human, self.name)]

    def staff_commands(self) -> list[StaffCommand]:
        async def _set(match: re.Match, actor: str, paused: bool) -> str:
            phone = re.sub(r"\D", "", match.group(1))
            row = first(await self.ctx.db.table("contacts").select("*").eq("phone", phone).limit(1).execute())
            if not row:
                return f"No contact found for {phone}."
            try:
                await set_ai_paused(self.ctx.db, row["id"], paused)
            except Exception as e:
                await self.ctx.events.error("HANDOFF_TOGGLE_FAILED", tenant_id=self.ctx.tenant_id, contact=phone[:6] + "****", paused=paused, error=str(e)[:200])
                return f"⚠️ I couldn't {'pause' if paused else 'resume'} the assistant for {phone} — nothing was confirmed. Please send the same message again."
            await self.ctx.events.info("HANDOFF_PAUSED" if paused else "HANDOFF_RESUMED", tenant_id=self.ctx.tenant_id, actor=actor[:6] + "****", contact=phone[:6] + "****")
            return f"{'Paused' if paused else 'Resumed'} the assistant for {phone}."

        async def resume(m, a): return await _set(m, a, False)
        async def pause(m, a): return await _set(m, a, True)
        return [StaffCommand("resume <number>", [r"^resume\s+(\+?\d{7,15})\s*$"], resume),
                StaffCommand("pause <number>", [r"^pause\s+(\+?\d{7,15})\s*$"], pause)]

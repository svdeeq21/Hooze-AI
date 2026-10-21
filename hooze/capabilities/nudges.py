"""Nudges: follow-ups for conversations that went quiet (pd's "Sales Progression Engine").

Extracted from: pd `follow_up_engine.py` — the best-engineered module in pd. Every safety
mechanism is kept because each one traces to a real incident:
  * kill switch (platform AND per-tenant) and DRY-RUN by default,
  * RESERVE-BEFORE-SEND: record last_nudge_at *before* sending, so a bookkeeping failure can cost
    a send but never cause the uncapped resend loop of 2026-08-13 (one lead got ~20 messages),
  * per-step cooldown measured from the last nudge (premature escalations of 2026-07-26),
  * opt-out / already-bought detection, sending window in the tenant's local time,
  * bounded sequence -> EXHAUSTED, and optional escalate-to-human for high-intent states.
Generalised: idle thresholds, templates and escalation come from the PLAYBOOK STATES
(`idle_after_hours`, `nudges`, `escalate_after_nudges`), not from `IDLE_THRESHOLD_HOURS`
keyed by real-estate state names. Templates live in config, not a `message_variants` table.
(Note: with Meta Cloud API, free-form nudges outside the 24h window need approved templates —
the channel adapter for that provider must enforce it; see docs.)
"""
from __future__ import annotations
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from hooze.capabilities.base import Capability, register
from hooze.kernel.db import first
from hooze.settings import get_settings

OPT_OUT = [r"\bstop (messaging|contacting|texting)\b", r"\bdon'?t (message|contact|text) me\b", r"\bremove me\b", r"\bunsubscribe\b", r"\bleave me alone\b"]
LOST = [r"\balready (bought|got one|rented|found)\b", r"\bfound (somewhere|something) else\b", r"\bno longer interested\b", r"\bnot interested anymore\b"]


@register
class NudgesCapability(Capability):
    name = "nudges"

    def _pb(self):
        return self.ctx.config.playbook

    async def _state(self, conv_id: str) -> dict:
        row = first(await self.ctx.db.table("nudge_state").select("*").eq("conversation_id", conv_id).limit(1).execute())
        if row:
            return row
        return (await self.ctx.db.table("nudge_state").insert({"conversation_id": conv_id, "status": "ACTIVE", "step": 0}).execute()).data[0]

    async def on_inbound(self, turn) -> None:
        """Runs BEFORE any AI work: a nudge already in flight this cycle must not double-send after the customer replies."""
        row = first(await turn.db.table("nudge_state").select("*").eq("conversation_id", turn.conversation["id"]).limit(1).execute())
        text = turn.text or ""
        new_status = "OPTED_OUT" if any(re.search(p, text, re.I) for p in OPT_OUT) else "LOST" if any(re.search(p, text, re.I) for p in LOST) else None
        if new_status:
            fields = {"status": new_status, "next_nudge_at": None}
            if row: await turn.db.table("nudge_state").update(fields).eq("id", row["id"]).execute()
            else: await turn.db.table("nudge_state").insert({"conversation_id": turn.conversation["id"], "step": 0, **fields}).execute()
            await turn.events.info("NUDGE_" + new_status, tenant_id=turn.tenant_id, trace_id=turn.trace_id)
        elif row and row["status"] in ("IDLE", "EXHAUSTED"):
            await turn.db.table("nudge_state").update({"status": "ACTIVE", "step": 0}).eq("id", row["id"]).execute()

    def _in_window(self, now: datetime) -> bool:
        start, end = self.config.get("window", [8, 20])
        local = now.astimezone(ZoneInfo(self.ctx.config.timezone))
        return start <= local.hour + local.minute / 60 < end

    async def tick(self) -> dict:
        s = get_settings()
        summary = {"evaluated": 0, "sent": 0, "dry_run": 0, "skipped": 0, "exhausted": 0, "escalated": 0}
        pb = self._pb()
        if not (s.nudges_globally_enabled and self.config.get("enabled", True) and pb):
            return {"status": "disabled", **summary}
        dry = self.config.get("dry_run", True)
        now = self.ctx.clock()
        eligible = {st.name: st for st in pb.states if st.idle_after_hours and not st.terminal}
        if not eligible:
            return {"status": "no_eligible_states", **summary}
        convs = (await self.ctx.db.table("conversations").select("*").in_("state", list(eligible)).eq("status", "active").execute()).data or []
        for conv in convs:
            st = eligible[conv["state"]]
            last_in = conv.get("last_inbound_at")
            if not last_in or now - datetime.fromisoformat(last_in) < timedelta(hours=st.idle_after_hours):
                continue
            summary["evaluated"] += 1
            contact = first(await self.ctx.db.table("contacts").select("*").eq("id", conv["contact_id"]).limit(1).execute())
            ns = await self._state(conv["id"])
            skip = (not contact or contact.get("ai_paused") or contact.get("opted_out") or ns["status"] in ("OPTED_OUT", "LOST", "EXHAUSTED")
                    or not self._in_window(now)
                    or (ns.get("last_nudge_at") and now - datetime.fromisoformat(ns["last_nudge_at"]) < timedelta(hours=st.idle_after_hours)))
            if skip:
                summary["skipped"] += 1
                continue
            step = ns.get("step", 0)
            if st.escalate_after_nudges is not None and step >= st.escalate_after_nudges:
                if not dry:
                    await self.ctx.outbox.notify_staff(f"⚠️ {contact.get('display_name') or contact['phone']} (+{contact['phone']}) hasn't replied after {step} follow-up(s) in stage {st.name}.")
                await self.ctx.db.table("nudge_state").update({"status": "EXHAUSTED"}).eq("id", ns["id"]).execute()
                summary["escalated"] += 1
                continue
            if step >= self.config.get("max_steps", 3):
                await self.ctx.db.table("nudge_state").update({"status": "EXHAUSTED"}).eq("id", ns["id"]).execute()
                summary["exhausted"] += 1
                continue
            if step >= len(st.nudges):
                await self.ctx.events.warn("NUDGE_NO_TEMPLATE", tenant_id=self.ctx.tenant_id, state=st.name, step=step)
                summary["skipped"] += 1
                continue
            slots = defaultdict(lambda: "", {**((contact.get("attributes") or {}).get("slots") or {}), "name": (contact.get("display_name") or "there").split()[0]})
            message = st.nudges[step].format_map(slots)
            if dry:
                await self.ctx.events.info("NUDGE_DRY_RUN_WOULD_SEND", tenant_id=self.ctx.tenant_id, state=st.name, step=step, preview=message[:120])
                await self.ctx.db.table("nudge_state").update({"step": step + 1, "last_nudge_at": now.isoformat(), "status": "IDLE"}).eq("id", ns["id"]).execute()
                summary["dry_run"] += 1
                continue
            # RESERVE BEFORE SEND — a failure here can cost a send, never cause a resend loop
            try:
                await self.ctx.db.table("nudge_state").update({"last_nudge_at": now.isoformat(), "status": "IDLE"}).eq("id", ns["id"]).execute()
            except Exception as e:
                await self.ctx.events.warn("NUDGE_RESERVE_FAILED", tenant_id=self.ctx.tenant_id, error=str(e)[:160])
                summary["skipped"] += 1
                continue
            try:
                await self.ctx.outbox.send(contact["phone"], message, conv["channel_id"], conversation_id=conv["id"])
            except Exception as e:
                await self.ctx.events.warn("NUDGE_SEND_FAILED", tenant_id=self.ctx.tenant_id, error=str(e)[:160])
                summary["skipped"] += 1
                continue
            try:
                await self.ctx.db.table("nudge_state").update({"step": step + 1}).eq("id", ns["id"]).execute()
            except Exception:
                pass   # cooldown already protects against a resend
            summary["sent"] += 1
        await self.ctx.events.info("NUDGE_CYCLE_COMPLETE", tenant_id=self.ctx.tenant_id, **summary)
        return {"status": "ok", **summary}

"""The inbound turn pipeline — ONE implementation for every tenant and vertical.

Replaces pd `message_pipeline.py` (3,800 lines, a 1,740-line `_process_inner`) and
savoury `message_pipeline.py` (500 lines). Both had the same skeleton:

  idempotency -> per-contact lock -> route staff vs customer -> deterministic gates
  -> LLM interpretation -> validated actions -> reply (with authoritative overrides)
  -> record -> follow-up bookkeeping

Everything vertical (property scoring, budget buckets, Abuja neighbourhoods, modifier
groups, delivery-fee rules) is NOT here; it lives in capabilities/config.
Ordering of concerns is deliberate:
  1. DB-level dedupe claim   2. paused/handoff short-circuit   3. deterministic gates
  4. LLM interpret           5. execute actions               6. guards
  7. send                    8. playbook advance + hooks
"""
from __future__ import annotations
import asyncio, re, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from hooze.channels.base import InboundMessage
from hooze.kernel.db import first
from hooze.kernel.locks import distributed_lock
from hooze.llm.gateway import LLMUnavailable
from hooze.runtime import guards
from hooze.runtime.actions import ToolRegistry, execute
from hooze.runtime.config import RuntimeConfig
from hooze.runtime.messenger import Messenger
from hooze.runtime.playbook import TurnFacts
from hooze.runtime.prompt import build_system_prompt, build_user_prompt
from hooze.runtime.store import ConversationStore
from hooze.runtime.turn import Turn
from hooze.runtime.tenant_runtime import build_tenant_runtime

DEFAULT_MEDIA_REPLIES: dict[str, Optional[str]] = {
    "image": "Thanks for sharing that! I can't view images here — could you describe what you need in text?",
    "video": "Thanks! I can't play videos here — could you describe what you need in text?",
    "document": "Thanks! I can't open files here — could you summarise what you need in text?",
    "audio": "I caught your voice note but couldn't make it out — could you type that for me?",
    "location": "Got the location, thank you! Tell me what you're looking for and I'll take it from there.",
    "contact": "Thanks for sharing that contact! Is there something I can help you with?",
    "sticker": None, "reaction": None,
    "other": "Sorry, I didn't quite catch that — could you type it out?",
}


@dataclass
class TurnOutcome:
    status: str
    reply: Optional[str] = None
    actions: list = field(default_factory=list)
    detail: str = ""


def normalize_phone(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


class ConfigLoader:
    """Loads the ACTIVE compiled RuntimeConfig for a tenant (small TTL cache)."""
    def __init__(self, platform_db, ttl: float = 30.0):
        self.pdb, self.ttl, self._cache = platform_db, ttl, {}

    def invalidate(self, tenant_id: str) -> None:
        self._cache.pop(tenant_id, None)

    async def load_active(self, tenant_id: str) -> Optional[RuntimeConfig]:
        hit = self._cache.get(tenant_id)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        rows = (await self.pdb.table("tenant_configs").select("*").eq("tenant_id", tenant_id).eq("status", "active").order("version", desc=True).limit(1).execute()).data or []
        cfg = RuntimeConfig(**rows[0]["compiled"]) if rows else None
        self._cache[tenant_id] = (time.monotonic(), cfg)
        return cfg


class Pipeline:
    def __init__(self, *, platform_db, llm, events, adapters, configs: ConfigLoader, settings,
                 clock: Optional[Callable[[], datetime]] = None, sleep=asyncio.sleep, transcriber=None,
                 extras_for: Optional[Callable[[str], dict]] = None, raise_errors: bool = False):
        self.pdb, self.llm, self.events, self.adapters, self.configs, self.settings = platform_db, llm, events, adapters, configs, settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep, self.transcriber, self.extras_for, self.raise_errors = sleep, transcriber, extras_for or (lambda tid: {}), raise_errors

    # ── public entry point ────────────────────────────────────────
    async def handle_inbound(self, channel: dict, inbound: InboundMessage, *, replay: bool = False, attempt: Optional[int] = None) -> TurnOutcome:
        """`replay=True`: this message came back from the durable queue after an interrupted attempt (see runtime/inbox.py)."""
        trace = uuid.uuid4().hex[:12]
        tenant_id = channel["tenant_id"]
        try:
            return await self._handle(channel, inbound, trace, replay, attempt)
        except Exception as e:
            await self.events.error("TURN_CRASHED", tenant_id=tenant_id, trace_id=trace, error=str(e)[:300])
            if self.raise_errors:
                raise
            return TurnOutcome("error", detail=str(e)[:200])

    async def _handle(self, channel: dict, inbound: InboundMessage, trace: str, replay: bool = False, attempt: Optional[int] = None) -> TurnOutcome:
        tenant_id = channel["tenant_id"]
        if inbound.from_me or inbound.is_group or not inbound.phone:
            return TurnOutcome("ignored", detail="own_group_or_no_phone")
        tenant = first(await self.pdb.table("tenants").select("*").eq("id", tenant_id).limit(1).execute())
        if not tenant or tenant.get("status") != "active":
            return TurnOutcome("ignored", detail="tenant_inactive")
        cfg = await self.configs.load_active(tenant_id)
        if cfg is None:
            await self.events.warn("NO_ACTIVE_CONFIG", tenant_id=tenant_id, trace_id=trace)
            return TurnOutcome("ignored", detail="no_active_config")

        db = self.pdb.for_tenant(tenant_id)
        store = ConversationStore(db, self.clock)
        adapter = self.adapters.for_channel(channel)
        ctx, caps = build_tenant_runtime(pdb=self.pdb, adapters=self.adapters, events=self.events, llm=self.llm, cfg=cfg,
                                         tenant_id=tenant_id, clock=self.clock, extras=self.extras_for(tenant_id))
        phone = normalize_phone(inbound.phone)

        async with distributed_lock(f"{tenant_id}:{phone}"):
            if phone in {normalize_phone(n) for n in cfg.staff_numbers}:
                return await self._staff(adapter, tenant_id, caps, inbound, phone, trace)
            return await self._customer(tenant, channel, adapter, db, store, cfg, caps, ctx, inbound, phone, trace, replay, attempt)

    # ── staff (owner/manager) route: deterministic, never the LLM ──
    async def _staff(self, adapter, tenant_id, caps, inbound, phone, trace) -> TurnOutcome:
        text = (inbound.text or "").strip()
        commands = [c for cap in caps.values() for c in cap.staff_commands()]
        for c in commands:
            for pat in c.patterns:
                m = re.match(pat, text, re.I)
                if m:
                    reply = await c.handler(m, phone)
                    await adapter.send_text(phone, reply, typing_delay=0.0)
                    await self.events.info("STAFF_COMMAND", tenant_id=tenant_id, trace_id=trace, command=c.name, actor=phone[:6] + "****")
                    return TurnOutcome("staff", reply=reply, detail=c.name)
        help_text = "I didn't recognise that. Commands: " + "; ".join(c.name for c in commands) if commands else "No staff commands are enabled."
        await adapter.send_text(phone, help_text, typing_delay=0.0)
        return TurnOutcome("staff", reply=help_text, detail="unknown")

    # ── customer route ────────────────────────────────────────────
    async def _customer(self, tenant, channel, adapter, db, store: ConversationStore, cfg: RuntimeConfig, caps, ctx, inbound, phone, trace, replay: bool = False, attempt: Optional[int] = None) -> TurnOutcome:
        tid = tenant["id"]
        t0 = time.perf_counter()
        contact, is_new = await store.get_or_create_contact(phone, inbound.push_name)
        initial = cfg.playbook.initial if cfg.playbook else "active"
        conv = await store.get_or_create_conversation(contact["id"], channel["id"], initial)

        text = (inbound.text or "").strip()
        content_type = inbound.kind if inbound.kind != "text" else "text"
        # voice notes -> transcript, then flows through the exact same pipeline as typed text
        if inbound.kind == "audio" and self.transcriber:
            audio = await adapter.download_media(inbound.raw)
            transcript = await self.transcriber(audio) if audio else None
            if transcript:
                text, content_type = transcript.strip(), "text"

        if not await store.claim_inbound(conv, contact["id"], channel["id"], inbound.provider_message_id, text or f"[{inbound.kind}]", content_type):
            # A genuine provider redelivery is ignored. BUT a message replayed from the queue after a crash is ALSO "already stored" while
            # the customer may still be waiting: carry on only if nothing has been sent in answer to it yet (never double-reply).
            if replay and inbound.provider_message_id and not await store.has_reply_after_inbound(conv["id"], inbound.provider_message_id):
                await self.events.info("INBOUND_REPLAYED", tenant_id=tid, trace_id=trace)
            else:
                await self.events.info("DUPLICATE_MESSAGE_IGNORED", tenant_id=tid, trace_id=trace)
                return TurnOutcome("duplicate")

        out = Messenger(adapter=adapter, store=store, to=phone, conversation_id=conv["id"], tenant_id=tid, events=self.events, trace_id=trace,
                        delay_range=(self.settings.typing_delay_min, self.settings.typing_delay_max), sleep=self.sleep)
        turn = Turn(tenant=tenant, channel=channel, contact=contact, conversation=conv, inbound=inbound, text=text, config=cfg, db=db, store=store,
                    out=out, events=self.events, llm=self.llm, now=self.clock(), trace_id=trace, is_new_contact=is_new,
                    state=conv.get("state") or initial, capabilities=caps)

        for cap in caps.values():                       # reset follow-up timers etc. BEFORE any AI work
            await cap.on_inbound(turn)

        if contact.get("ai_paused") or conv.get("status") == "handoff":
            return TurnOutcome("paused")                 # a human owns this conversation; message is stored

        if content_type != "text" or not text:           # media we can't interpret
            replies = {**DEFAULT_MEDIA_REPLIES, **cfg.media_replies}
            canned = replies.get(inbound.kind, replies["other"])
            if canned:
                await out.text(canned, typing=False)
            return TurnOutcome("media", reply=canned)

        greeted = False
        if is_new and cfg.first_contact_message:
            await out.text(cfg.first_contact_message, typing=False)
            greeted = True

        if await store.count_recent_bot(conv["id"]) >= cfg.max_bot_replies_per_minute:
            await self.events.warn("RATE_LIMITED", tenant_id=tid, trace_id=trace)
            return TurnOutcome("gated", detail="rate_limited")

        hist = await store.recent_messages(conv["id"], cfg.history_window)
        if hist and hist[-1]["sender_type"] == "contact" and hist[-1]["content"] == text:
            hist = hist[:-1]                       # the latest message is passed separately in the prompt
        turn.history = hist

        # 3) deterministic gates (closed hours, payment pending, ...) — the LLM never sees these turns
        for cap in caps.values():
            g = await cap.gate(turn)
            if g and g.handled:
                if g.reply and not (g.dedupe and guards.is_repeat_of_last_bot(turn.history, g.reply)):
                    await out.text(g.reply)
                return TurnOutcome("gated", reply=g.reply, detail=cap.name)

        # 4) interpret
        registry = ToolRegistry()
        for cap in caps.values():
            for tool in cap.tools():
                registry.register(tool)
        t1 = time.perf_counter()
        contexts = [await cap.context(turn) for cap in caps.values()]
        turn.timings["context_ms"] = round((time.perf_counter() - t1) * 1000)
        turn.greeting_sent = greeted
        system, prompt = build_system_prompt(turn, contexts, registry.describe()), build_user_prompt(turn)
        t2 = time.perf_counter()
        try:
            res = await self.llm.interpret(tid, system=system, prompt=prompt)
        except LLMUnavailable:
            # EVERY provider failed. Escalating at once paused the customer's assistant and flooded staff for what is usually a short
            # outage. With the durable queue we can do better: hold the customer once, let the queue retry (with backoff), and only hand
            # over to a human if the outage persists. Direct calls without a queue (attempt=None) keep the old immediate handover.
            last = attempt is None or attempt >= getattr(self.settings, "llm_outage_attempts", 3)
            if last:
                await caps["handoff"].escalate(turn, "LLM_ALL_PROVIDERS_FAILED")
                await out.text("Give me a moment — a member of the team will pick this up shortly.", typing=False)
                return TurnOutcome("handoff", detail="llm_unavailable")
            await self.events.warn("LLM_UNAVAILABLE_RETRY", tenant_id=tid, trace_id=trace, attempt=attempt)
            if attempt == 1:
                await out.hold("One moment — I'll be right with you.")
            return TurnOutcome("error", detail="llm_unavailable")
        turn.timings["llm_ms"] = round((time.perf_counter() - t2) * 1000)
        interp = res.interpretation
        turn.intent = interp.intent

        # 5) execute proposed actions deterministically
        t3 = time.perf_counter()
        execution = await execute(turn, interp.actions, registry)
        turn.results = execution.results
        turn.timings["tools_ms"] = round((time.perf_counter() - t3) * 1000)

        # 6) choose + guard the reply
        reply = execution.override() or interp.reply
        overridden = execution.override() is not None
        if not overridden:
            for cap in caps.values():
                fixed = cap.check_reply(turn, reply, turn.results)
                if fixed is not None:
                    reply = fixed
            if guards.looks_like_leak(reply):
                await self.events.warn("REPLY_LEAK_BLOCKED", tenant_id=tid, trace_id=trace, preview=reply[:80])
                reply = "Sorry — could you say that again? I want to make sure I get it right."
            last_bot = next((m["content"] for m in reversed(turn.history) if m["sender_type"] == "bot"), "")
            if last_bot and guards.too_similar(reply, last_bot):
                await self.events.info("REPLY_TOO_SIMILAR", tenant_id=tid, trace_id=trace)
            if not reply.rstrip().endswith("?"):
                for cap in caps.values():
                    step = await cap.next_step(turn)
                    if step:
                        reply = f"{reply} {step}".strip()
                        break

        # 7) send
        t4 = time.perf_counter()
        if reply and greeted and interp.intent == "greeting" and not execution.results:
            await self.events.info("GREETING_REPLY_SUPPRESSED", tenant_id=tid, trace_id=trace)      # the welcome IS the answer to "Hello"
        elif reply:
            await out.text(reply)
        turn.timings["send_ms"] = round((time.perf_counter() - t4) * 1000)

        # 8) advance the playbook, run hooks, maybe fold old messages into the summary
        await self._advance_playbook(turn, cfg, caps, store)
        for cap in caps.values():
            await cap.after_turn(turn)
        await self._maybe_summarise(turn, store)
        turn.timings["total_ms"] = round((time.perf_counter() - t0) * 1000)
        await self.events.info("TURN_COMPLETED", tenant_id=tid, trace_id=trace, provider=res.provider, degraded=res.degraded,
                               intent=turn.intent, state=turn.state, tools=[r.tool for r in turn.results], timings=turn.timings)
        return TurnOutcome("replied", reply=reply, actions=[a.function for a in interp.actions])

    async def _advance_playbook(self, turn: Turn, cfg: RuntimeConfig, caps, store) -> None:
        if not cfg.playbook:
            return
        qual = caps.get("qualification")
        facts = TurnFacts(intent=turn.intent, text=turn.text, slots_complete=bool(qual and qual.is_complete(turn.contact)),
                          tools_ok={r.tool for r in turn.results if r.ok}, user_message_count=turn.conversation.get("user_message_count", 0))
        current = turn.conversation.get("state") or cfg.playbook.initial
        nxt = cfg.playbook.next_state(current, facts)
        if nxt != current:
            await store.set_conversation(turn.conversation["id"], state=nxt)
            turn.conversation["state"] = nxt
            await self.events.info("STATE_TRANSITION", tenant_id=turn.tenant_id, trace_id=turn.trace_id, ref_type="conversation",
                                   ref_id=turn.conversation["id"], from_state=current, to_state=nxt, intent=turn.intent)
        turn.state = nxt

    async def _maybe_summarise(self, turn: Turn, store) -> None:
        """Rolling summary (pd audit C3): everything older than the window is folded into a summary, never re-sent verbatim."""
        interval, window = self.settings.summary_interval, turn.config.history_window
        n_user = turn.conversation.get("user_message_count", 0)
        if n_user == 0 or n_user % interval != 0:
            return
        total = await store.count_messages(turn.conversation["id"])
        boundary, done = max(0, total - window), turn.conversation.get("summary_upto", 0)
        if boundary <= done:
            return
        chunk = await store.messages_range(turn.conversation["id"], done, boundary - 1)
        if not chunk:
            return
        transcript = "\n".join(f"{'Customer' if m['sender_type'] == 'contact' else 'You'}: {m['content']}" for m in chunk)
        existing = turn.conversation.get("summary") or ""
        summary = await self.llm.summarize(turn.tenant_id, (f"EXISTING SUMMARY:\n{existing}\n\nNEW MESSAGES:\n" if existing else "") + transcript)
        if summary:
            await store.set_conversation(turn.conversation["id"], summary=summary, summary_upto=boundary)
            turn.conversation.update(summary=summary, summary_upto=boundary)
            await self.events.info("SUMMARY_UPDATED", tenant_id=turn.tenant_id, trace_id=turn.trace_id, upto=boundary)

"""Operator alerts: tell a human when something is broken, once, in plain language, with what to do about it.

Watches the platform event stream (EventBus sink). A rule matches event names, optionally needs N occurrences inside a window
("repeating" problems), and is de-duplicated per (rule, client) so a flapping fault produces one message per window, not hundreds.
Delivery never blocks or breaks a customer's turn: it runs as a background task and every failure is swallowed (and logged).

What this cannot do: tell you the whole app is DOWN (it lives inside the app). Point an external uptime monitor at /health/ready for that.
"""
from __future__ import annotations
import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

from hooze.kernel.db import first
from hooze.kernel.errors import describe_exc
from hooze.kernel.phone import normalize_phone

_log = logging.getLogger("hooze")
LAGOS = ZoneInfo("Africa/Lagos")


@dataclass(frozen=True)
class Rule:
    id: str
    events: frozenset
    title: str
    advice: str
    window_s: int = 1800               # send at most one alert per (rule, client) per window
    threshold: int = 1                 # needs this many matching events ...
    within_s: int = 600                # ... inside this many seconds
    icon: str = "🔴"


def _r(id, events, title, advice, **kw) -> Rule:
    return Rule(id, frozenset(events.split()), title, advice, **kw)


RULES: list[Rule] = [
    _r("inbound_failed", "INBOUND_FAILED", "A customer's message could not be processed",
       "Open the dashboard → Overview → Message queue and press Retry. If it keeps failing, check Events for the error.", window_s=600),
    _r("turn_crashed", "TURN_CRASHED INBOUND_PROCESS_ERROR", "The assistant crashed while handling a message",
       "The message is retried automatically. If this repeats, open Events and send me the error text.", window_s=900),
    _r("money_problem", "PAYMENT_UNKNOWN_REFERENCE PAYMENT_AMOUNT_MISMATCH PAYMENT_CONFIRM_FAILED CHECKOUT_INIT_FAILED",
       "A payment needs your attention", "Money may have been taken without the order updating. Open the client's Orders and the Paystack dashboard and match the reference.", window_s=300),
    _r("late_payment", "PAYMENT_AFTER_EXPIRED PAYMENT_AFTER_CANCELLED", "A customer paid after their checkout expired or was cancelled",
       "The order was marked paid and the business was told. Confirm it can be fulfilled, otherwise refund in Paystack.", window_s=300, icon="🟠"),
    _r("staff_alert_failed", "STAFF_ALERT_FAILED", "The business owner could not be notified",
       "New bookings/orders are not reaching them. Check the staff phone number under the client's Setup and that WhatsApp is connected.", window_s=1800),
    _r("no_config", "NO_ACTIVE_CONFIG", "A client has no live setup, so their customers are being ignored",
       "Open the client → Setup and press Go live.", window_s=3600),
    _r("channel_down", "CHANNEL_DISCONNECTED", "A WhatsApp number is disconnected",
       "Open the client → WhatsApp and scan the QR code again. Until then the assistant cannot reply.", window_s=1800),
    _r("webhook_failed", "WEBHOOK_SET_FAILED", "Evolution could not be pointed at the platform",
       "Open the client → WhatsApp → Reconnect webhook and check the address Evolution should use.", window_s=1800, icon="🟠"),
    _r("tick_failed", "TICK_FAILED", "The follow-up/expiry sweep failed", "Check Events for TICK_FAILED details.", window_s=1800, icon="🟠"),
    _r("llm_all_down", "LLM_UNAVAILABLE_RETRY", "Every AI provider is failing: customers are being held",
       "Customers get a holding message and are retried automatically, then handed to a human. Run `python -m scripts.llm_check` and check the keys/quotas.",
       threshold=3, within_s=600, window_s=1800),
    _r("llm_primary_down", "LLM_FALLBACK_USED", "The primary AI provider keeps failing (using the backup)",
       "Replies are slower. Run `python -m scripts.llm_check`, then check the provider key, quota and model name.",
       threshold=3, within_s=600, window_s=3600, icon="🟠"),
    _r("knowledge_down", "KNOWLEDGE_LOOKUP_FAILED", "Knowledge lookup keeps failing",
       "The assistant can't quote business facts reliably. Check the Gemini embedding model/quota and Supabase.", threshold=5, within_s=600, window_s=3600, icon="🟠"),
    _r("llm_garbage", "LLM_UNPARSEABLE_RESPONSE", "The AI keeps returning unusable output",
       "Check the model name and token limits (LLM settings).", threshold=5, within_s=900, window_s=3600, icon="🟠"),
    _r("webhook_auth", "WEBHOOK_AUTH_FAILED PAYMENT_WEBHOOK_AUTH_FAILED", "Webhook calls are being rejected",
       "Either a webhook secret is out of date (use Reconnect webhook) or someone is probing the address.", threshold=5, within_s=600, window_s=3600, icon="🟠"),
    _r("nudge_failed", "NUDGE_SEND_FAILED", "Follow-up messages are failing to send", "Check the client's WhatsApp connection.", threshold=3, within_s=1800, window_s=3600, icon="🟠"),
]
_BY_EVENT: dict[str, Rule] = {e: r for r in RULES for e in r.events}


class Sink:
    name = "sink"
    def describe(self) -> str: return self.name
    async def send(self, title: str, body: str) -> None: raise NotImplementedError


class WhatsAppAlertSink(Sink):
    name = "whatsapp"

    def __init__(self, pdb, adapters, numbers: list[str], channel_id: str = ""):
        self.pdb, self.adapters, self.numbers, self.channel_id = pdb, adapters, numbers, channel_id

    def describe(self) -> str:
        return f"WhatsApp → {len(self.numbers)} number(s)"

    async def _candidates(self) -> list[dict]:
        """Active WhatsApp numbers, CONNECTED ones first (the configured one leading). A stale or logged-out instance used to be picked just
        because it was the oldest row, and every alert then failed."""
        chans = (await self.pdb.table("channels").select("*").eq("status", "active").order("created_at").execute()).data or []

        async def live(ch: dict) -> bool:
            try:
                return (await asyncio.wait_for(self.adapters.for_channel(ch).connection_status(), 5.0)).lower() in ("open", "connected")
            except Exception:
                return False
        flags = await asyncio.gather(*[live(c) for c in chans])
        ranked = sorted(zip(chans, flags), key=lambda cf: (not cf[1], cf[0]["id"] != self.channel_id))
        return [c for c, _ in ranked]

    async def send(self, title: str, body: str) -> None:
        chans = await self._candidates()
        if not chans:
            raise RuntimeError("no active WhatsApp channel to send alerts from")
        last_errors: list[str] = []
        for ch in chans:                                       # first number that can actually deliver wins
            adapter, errors = self.adapters.for_channel(ch), []
            for n in self.numbers:
                try:
                    await adapter.send_text(n, body, typing_delay=0.0)
                except Exception as e:
                    errors.append(f"{n[-4:]}: {describe_exc(e, 80)}")
            if len(errors) < len(self.numbers):
                return
            last_errors += [f"{ch['instance_name']}: {e}" for e in errors]
        raise RuntimeError("; ".join(last_errors))


class EmailAlertSink(Sink):
    name = "email"

    def __init__(self, api_key: str, to: list[str], sender: str, http_factory: Optional[Callable[..., Any]] = None):
        self.api_key, self.to, self.sender = api_key, to, sender
        self._http = http_factory or (lambda timeout=15.0: httpx.AsyncClient(timeout=timeout))

    def describe(self) -> str:
        return f"Email → {len(self.to)} address(es)"

    async def send(self, title: str, body: str) -> None:
        async with self._http(15.0) as c:
            r = await c.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                             json={"from": self.sender, "to": self.to, "subject": title[:150], "text": body})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Resend {r.status_code}: {r.text[:150]}")


def build_sinks(settings, pdb, adapters) -> list[Sink]:
    sinks: list[Sink] = []
    nums = [n for n in (normalize_phone(x) for x in settings.alert_whatsapp_numbers.split(",")) if n]
    if nums:
        sinks.append(WhatsAppAlertSink(pdb, adapters, nums, settings.alert_channel_id.strip()))
    to = [e.strip() for e in settings.alert_email_to.split(",") if e.strip()]
    if to and settings.resend_api_key:
        sinks.append(EmailAlertSink(settings.resend_api_key, to, settings.alert_email_from))
    return sinks


class AlertManager:
    """An EventBus sink. `inline=True` (tests) delivers before returning instead of in a background task."""

    def __init__(self, sinks: list[Sink], clock: Callable[[], datetime], name_of: Optional[Callable[[str], Awaitable[str]]] = None,
                 dedupe_window_s: Optional[int] = None, inline: bool = False):
        self.sinks, self.clock, self.name_of, self.inline = sinks, clock, name_of, inline
        self.dedupe_override = dedupe_window_s
        self.bus: Any = None                                    # set after construction (the bus holds this manager as a sink)
        self._hits: dict[tuple, deque] = defaultdict(deque)
        self._last_sent: dict[tuple, float] = {}
        self._suppressed: dict[tuple, int] = defaultdict(int)
        self._tasks: set[asyncio.Task] = set()

    def describe(self) -> list[str]:
        return [s.describe() for s in self.sinks]

    async def __call__(self, e: dict) -> None:
        rule = _BY_EVENT.get(e.get("event", ""))
        if not rule:
            return
        now = self.clock().timestamp()
        key = (rule.id, e.get("tenant_id") or "-", (e.get("metadata") or {}).get("channel_id") or "")
        if rule.threshold > 1:
            hits = self._hits[key]
            hits.append(now)
            while hits and now - hits[0] > rule.within_s:
                hits.popleft()
            if len(hits) < rule.threshold:
                return
        window = self.dedupe_override or rule.window_s
        if now - self._last_sent.get(key, -1e18) < window:
            self._suppressed[key] += 1
            return
        self._last_sent[key] = now
        extra = self._suppressed.pop(key, 0)
        if not self.sinks:
            return                                              # raised but nowhere to send: the Overview page says "alerts not configured"
        if rule.threshold > 1:
            self._hits[key].clear()
        coro = self._deliver(rule, e, extra)
        if self.inline:
            await coro
        else:
            t = asyncio.create_task(coro)
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)

    async def _compose(self, rule: Rule, e: dict, suppressed: int) -> tuple[str, str]:
        md = e.get("metadata") or {}
        who = None
        if e.get("tenant_id") and self.name_of:
            try:
                who = await self.name_of(e["tenant_id"])
            except Exception:
                who = None
        lines = [f"{rule.icon} Hooze alert: {rule.title}"]
        if e.get("tenant_id"):
            lines.append(f"Client: {who or str(e['tenant_id'])[:8]}")
        detail = md.get("error") or md.get("state") or md.get("reference")
        if detail:
            lines.append(f"Detail: {str(detail)[:200]}")
        if rule.threshold > 1:
            lines.append(f"Seen {rule.threshold}+ times in {rule.within_s // 60} min.")
        lines.append(f"Do: {rule.advice}")
        if suppressed:
            lines.append(f"({suppressed} similar alert(s) held back in the last {(self.dedupe_override or rule.window_s) // 60} min)")
        lines.append(self.clock().astimezone(LAGOS).strftime("%d %b %H:%M") + " (Lagos)")
        return rule.title, "\n".join(lines)

    async def _deliver(self, rule: Rule, e: dict, suppressed: int) -> list[dict]:
        try:
            title, body = await self._compose(rule, e, suppressed)
            return await self._send_all(title, body, rule_id=rule.id, tenant_id=e.get("tenant_id"))
        except Exception as exc:                                # alerting must never raise into the caller
            _log.warning(f"alert delivery crashed: {exc}")
            return []

    async def _send_all(self, title: str, body: str, *, rule_id: str, tenant_id: Optional[str] = None) -> list[dict]:
        async def one(s: Sink) -> dict:
            try:
                await s.send(title, body)
                return {"sink": s.describe(), "ok": True}
            except Exception as exc:
                return {"sink": s.describe(), "ok": False, "error": str(exc)[:200]}
        results = await asyncio.gather(*[one(s) for s in self.sinks])
        if self.bus:
            if any(r["ok"] for r in results):
                await self.bus.info("ALERT_SENT", tenant_id=tenant_id, rule=rule_id, sinks=[r["sink"] for r in results if r["ok"]])
            else:
                await self.bus.warn("ALERT_DELIVERY_FAILED", tenant_id=tenant_id, rule=rule_id, errors=[r.get("error") for r in results])
        return results

    async def send_test(self) -> list[dict]:
        if not self.sinks:
            return []
        body = f"✅ Hooze test alert\nIf you can read this, alerts reach you.\n{self.clock().astimezone(LAGOS).strftime('%d %b %H:%M')} (Lagos)"
        return await self._send_all("Hooze test alert", body, rule_id="test")

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

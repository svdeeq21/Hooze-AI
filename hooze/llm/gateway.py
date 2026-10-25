"""LLM gateway: ONE place that talks to models.

Extracted from: pd `llm.py` (provider fallback chain, too-similar retry, summary)
+ savoury `ordering_llm.py` (strict JSON action contract, tolerant JSON parsing).

Changes:
  * Providers are pluggable objects; Gemini / Groq imported lazily.
  * Everything is `async` and time-boxed. (savoury calls the *sync* Gemini client
    inside `async def` in its hot path — the same blocking-event-loop bug as pd's
    audit C2. There is no sync path here to get wrong.)
  * Every call is metered per tenant (`usage`), which is what makes the
    product billable.
  * No prompts live here. Prompts are assembled by the runtime from
    persona + playbook + capability context.
"""
from __future__ import annotations
import asyncio, json, re, time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

import httpx

from hooze.runtime.models import Interpretation


class LLMUnavailable(Exception):
    """Every provider failed. The pipeline turns this into a human handoff, never a silent drop."""


class LLMProvider(Protocol):
    name: str
    async def generate(self, *, system: str, prompt: str, json_mode: bool, max_tokens: int, temperature: float) -> str: ...
    async def embed(self, text: str, dims: int, kind: str = "query") -> list[float]: ...


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, embedding_model: str):
        self.api_key, self.model, self.embedding_model, self._client = api_key, model, embedding_model, None

    def _c(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def generate(self, *, system, prompt, json_mode, max_tokens, temperature) -> str:
        from google.genai import types
        cfg = types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens, temperature=temperature,
                                          response_mime_type="application/json" if json_mode else None)
        resp = await self._c().aio.models.generate_content(model=self.model, contents=prompt, config=cfg)
        text = (resp.text or "").strip()
        if not text:
            raise ValueError("gemini returned empty response")
        return text

    async def embed(self, text: str, dims: int, kind: str = "query") -> list[float]:
        """kind="document" for text being INDEXED, "query" for the customer's question: Gemini embeds the two differently on purpose,
        so indexing with the query type (what this used to do) lowers match quality."""
        from google.genai import types
        task = "RETRIEVAL_DOCUMENT" if kind == "document" else "RETRIEVAL_QUERY"
        r = await self._c().aio.models.embed_content(model=self.embedding_model, contents=text,
                                                     config=types.EmbedContentConfig(task_type=task, output_dimensionality=dims))
        return list(r.embeddings[0].values)


class OpenAICompatProvider:
    """Groq / OpenRouter / any OpenAI-compatible endpoint."""
    def __init__(self, name: str, base_url: str, api_key: str, model: str, reasoning_effort: str = "", http_factory: Optional[Callable[..., Any]] = None):
        self.name, self.base_url, self.api_key, self.model = name, base_url.rstrip("/"), api_key, model
        self.reasoning_effort = reasoning_effort                     # "low" => gpt-oss thinks briefly: faster, and never spends the whole budget before answering
        self._http = http_factory or (lambda timeout=30.0: httpx.AsyncClient(timeout=timeout))

    async def generate(self, *, system, prompt, json_mode, max_tokens, temperature) -> str:
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        async with self._http(30.0) as c:
            r = await c.post(f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        if r.status_code != 200:
            raise RuntimeError(f"{self.name} {r.status_code}: {r.text[:200]}")
        choice = r.json()["choices"][0]
        text = (choice["message"]["content"] or "").strip()
        if not text:
            raise ValueError(f"{self.name} returned empty response (finish_reason={choice.get('finish_reason')}). A reasoning model can spend its whole "
                             f"token budget thinking before it answers: set GROQ_REASONING_EFFORT=low (or raise the token limit).")
        return text

    async def embed(self, text: str, dims: int, kind: str = "query") -> list[float]:
        raise NotImplementedError(f"{self.name} does not provide embeddings")


def parse_interpretation(raw: str) -> Optional[Interpretation]:
    """Tolerant parse: strips ``` fences, falls back to the first {...} block. None if unusable."""
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    block = re.search(r"\{.*\}", cleaned, re.DOTALL)
    for candidate in (cleaned, block.group(0) if block else None):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("reply"), str):
            actions = []
            for a in data.get("actions") or []:
                if isinstance(a, dict) and a.get("function"):
                    fn = a["function"]
                    actions.append({"function": fn, "args": {k: v for k, v in a.items() if k != "function"}})
            try:
                return Interpretation(actions=actions, reply=data["reply"], intent=data.get("intent"))
            except Exception:
                return None
    return None


@dataclass
class InterpretResult:
    interpretation: Interpretation
    provider: str
    degraded: bool = False   # True => model answered but not in the contract; we fell back to a safe reply


UsageRecorder = Callable[[str, str, float, dict], Awaitable[None]]

REPAIR_SUFFIX = "\n\nIMPORTANT: your previous answer was not valid JSON. Reply with ONLY the JSON object described above: no commentary, no code fences."


INTERPRET_TOKENS = 900
INTERPRET_RETRY_TOKENS = 1800     # the retry exists mostly for models that ran out of budget (a reasoning model "thinking" through it): same budget again fails the same way


class LLMGateway:
    """Provider chain with a per-provider COOLDOWN (a small circuit breaker).

    Without it, every turn re-tried a dead/slow primary and paid its full timeout before reaching the fallback (the real
    24s turn in the first live test), and the knowledge lookup (embed) paid it AGAIN before interpretation. Now a provider that
    just failed is skipped for `provider_cooldown` seconds. Policy differs by purpose, on purpose:
      * generate: if EVERY provider is cooling we still try them all. Failing closed here escalates customers to a human and
        PAUSES the AI for them, which is far worse than one slow turn.
      * embed (live knowledge lookup, `fail_fast=True`): fail fast; the pipeline already degrades to "lookup unavailable".
    """
    def __init__(self, providers: list[LLMProvider], *, timeout: float = 20.0, embedding_dims: int = 768,
                 usage: Optional[UsageRecorder] = None, events=None, provider_cooldown: float = 30.0,
                 now: Callable[[], float] = time.monotonic):
        self.providers, self.timeout, self.dims, self._usage, self.events = providers, timeout, embedding_dims, usage, events
        self.cooldown, self._now, self._cool = provider_cooldown, now, {}

    def status(self) -> list[dict]:
        """For the operator health panel: which providers are configured and whether any is being skipped right now."""
        t = self._now()
        left = lambda p, k: max(0, round(self._cool.get((p.name, k), 0.0) - t))
        return [{"name": p.name, "model": getattr(p, "model", None), "cooling_generate_s": left(p, "gen"), "cooling_embed_s": left(p, "embed")} for p in self.providers]

    def _cooling(self, p, kind: str) -> bool:
        return self._cool.get((p.name, kind), 0.0) > self._now()

    def _trip(self, p, kind: str) -> None:
        if self.cooldown > 0:
            self._cool[(p.name, kind)] = self._now() + self.cooldown

    def _clear(self, p, kind: str) -> None:
        self._cool.pop((p.name, kind), None)

    async def _meter(self, tenant_id: str, kind: str, provider: str, purpose: str, ms: float) -> None:
        if self._usage:
            try:
                await self._usage(tenant_id, kind, 1.0, {"provider": provider, "purpose": purpose, "latency_ms": round(ms)})
            except Exception:
                pass

    async def generate(self, tenant_id: str, *, system: str, prompt: str, json_mode: bool = False, max_tokens: int = 600,
                       temperature: float = 0.6, purpose: str = "reply", exclude: frozenset = frozenset()) -> tuple[str, str]:
        failed: list[str] = []
        loop = asyncio.get_event_loop()
        pool = [p for p in self.providers if p.name not in exclude] or list(self.providers)      # `exclude` is a preference, never a reason to fail
        candidates = [p for p in pool if not self._cooling(p, "gen")] or pool                  # all cooling => still try
        for p in candidates:
            t0 = loop.time()
            try:
                text = await asyncio.wait_for(p.generate(system=system, prompt=prompt, json_mode=json_mode,
                                                         max_tokens=max_tokens, temperature=temperature), self.timeout)
                self._clear(p, "gen")
                await self._meter(tenant_id, "llm_call", p.name, purpose, (loop.time() - t0) * 1000)
                if failed and self.events:
                    await self.events.warn("LLM_FALLBACK_USED", tenant_id=tenant_id, provider=p.name, failed=failed)
                return text, p.name
            except Exception as e:
                failed.append(p.name)
                self._trip(p, "gen")
                if self.events:
                    await self.events.warn("LLM_PROVIDER_FAILED", tenant_id=tenant_id, provider=p.name, error=(str(e) or type(e).__name__)[:160],
                                           cooldown_s=self.cooldown)
        raise LLMUnavailable(f"all providers failed: {failed}")

    async def interpret(self, tenant_id: str, *, system: str, prompt: str) -> InterpretResult:
        """Strict-JSON turn interpretation. A parse failure is NEVER turned into an action.

        If the first answer is not usable JSON (a reasoning model that ran out of tokens, a model that chatted instead of answering in the
        contract), ask once more, preferring a DIFFERENT provider, with a stricter instruction. The fallback below used to go straight to
        the customer as "could you rephrase that?" - which blames the customer for a model fault ("Yes i would" was never unclear).
        """
        text, provider = await self.generate(tenant_id, system=system, prompt=prompt, json_mode=True, max_tokens=INTERPRET_TOKENS, purpose="interpret")
        parsed = parse_interpretation(text)
        if parsed is None:
            if self.events:
                await self.events.warn("LLM_UNPARSEABLE_RESPONSE", tenant_id=tenant_id, provider=provider, raw=text[:300])
            try:
                text2, provider2 = await self.generate(tenant_id, system=system, prompt=prompt + REPAIR_SUFFIX, json_mode=True, max_tokens=INTERPRET_RETRY_TOKENS,
                                                       temperature=0.2, purpose="interpret_retry", exclude=frozenset({provider}))
                parsed = parse_interpretation(text2)
                if parsed is not None:
                    return InterpretResult(parsed, provider2)
                if self.events:
                    await self.events.warn("LLM_UNPARSEABLE_RESPONSE", tenant_id=tenant_id, provider=provider2, raw=text2[:300], retry=True)
            except LLMUnavailable:
                pass
            return InterpretResult(Interpretation(reply="Sorry, I didn't catch that properly. Could you send it once more?"), provider, degraded=True)
        return InterpretResult(parsed, provider)

    async def summarize(self, tenant_id: str, transcript: str) -> str:
        try:
            text, _ = await self.generate(tenant_id, system="You compress conversations. Be factual; keep names, numbers, preferences, decisions.",
                                          prompt=f"Update the running summary (3-6 sentences).\n\n{transcript}\n\nSummary:", max_tokens=300, temperature=0.2, purpose="summary")
            return text
        except LLMUnavailable:
            return ""

    async def embed(self, tenant_id: str, text: str, *, fail_fast: bool = False, kind: str = "query") -> list[float]:
        """`fail_fast=True` (live turns) skips providers whose embedding just failed. Activation leaves it False: seeding must try
        every document rather than abort a whole sync because one call blipped."""
        last: Exception | None = None
        for p in self.providers:
            if fail_fast and self._cooling(p, "embed"):
                last = last or RuntimeError(f"{p.name} embedding cooling down")
                continue
            try:
                v = await asyncio.wait_for(p.embed(text, self.dims, kind), self.timeout)
                self._clear(p, "embed")
                await self._meter(tenant_id, "embedding", p.name, "embed", 0)
                return v
            except NotImplementedError:
                continue
            except Exception as e:
                last = e
                self._trip(p, "embed")
        raise LLMUnavailable(f"no embedding provider available: {last}")


def build_default_gateway(settings, usage: Optional[UsageRecorder] = None, events=None) -> LLMGateway:
    available: dict[str, LLMProvider] = {}
    if settings.gemini_api_key:
        available["gemini"] = GeminiProvider(settings.gemini_api_key, settings.gemini_model, settings.embedding_model)
    if settings.groq_api_key:
        available["groq"] = OpenAICompatProvider("groq", "https://api.groq.com/openai/v1", settings.groq_api_key, settings.groq_model,
                                                 reasoning_effort=settings.groq_reasoning_effort.strip())
    wanted = [n.strip().lower() for n in settings.llm_provider_order.split(",") if n.strip()]
    ordered = [available[n] for n in dict.fromkeys(wanted) if n in available]
    ordered += [p for n, p in available.items() if n not in {x.name for x in ordered}]       # never drop a configured provider just because the list forgot it
    return LLMGateway(ordered, timeout=settings.llm_timeout_seconds, embedding_dims=settings.embedding_dims, usage=usage, events=events,
                      provider_cooldown=settings.llm_provider_cooldown_seconds)

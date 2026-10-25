"""Which AI provider should answer first? Measures each one with a REAL turn and says what to put in .env.

    python -m scripts.llm_check                              # test what .env has
    python -m scripts.llm_check --list-models                # also list the Gemini models your key can use
    python -m scripts.llm_check --gemini-model gemini-2.5-flash --gemini-model gemini-2.5-flash-lite   # try other Gemini models

Each provider gets (1) a tiny reply and (2) a realistic turn: the same strict-JSON contract the assistant uses, parsed the same way.
Groq's gpt-oss is also tried with reasoning_effort=low, because that setting often decides between a 3s answer and an empty one.
Prints no keys. A provider is only recommended if the realistic turn actually parsed.
"""
import argparse
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from hooze.kernel.errors import describe_exc
from hooze.llm.gateway import GeminiProvider, OpenAICompatProvider, parse_interpretation
from hooze.settings import get_settings

LIMIT = 25.0
SYSTEM = ("You are a WhatsApp sales assistant for a game store. Respond with ONLY a JSON object, no other text: "
          '{"actions": [], "reply": "<short message to the customer>", "intent": "<one of: greeting, question, buying, other>"}')
PROMPT = "LATEST MESSAGE FROM Ada:\nhow much is FIFA 26 on ps5 and can i pay today?"


@dataclass
class Result:
    label: str
    ok: bool
    seconds: float
    detail: str = ""


@dataclass
class ProviderReport:
    name: str
    model: str
    results: list = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results if r.label != "embedding")

    @property
    def embedding_problem(self) -> Optional[str]:
        """Embeddings are optional PER PROVIDER (Groq has none), so they do not make a provider 'unhealthy' for chat, but a provider that
        HAS an embedding model and fails it means knowledge/FAQ lookups are down. That must never pass silently (a retired model did)."""
        bad = [r for r in self.results if r.label == "embedding" and not r.ok]
        return bad[0].detail if bad else None

    @property
    def turn_seconds(self) -> Optional[float]:
        t = [r for r in self.results if r.label == "realistic turn" and r.ok]
        return t[0].seconds if t else None


async def _timed(label: str, coro, check=None) -> Result:
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(coro, LIMIT)
    except NotImplementedError:
        return Result(label, True, 0.0, "not supported by this provider (expected)")
    except asyncio.TimeoutError:
        return Result(label, False, LIMIT, f"TimeoutError: no answer within {LIMIT:.0f}s (the provider accepted the call but never replied)")
    except Exception as e:
        return Result(label, False, time.monotonic() - t0, f"{type(e).__name__}: {describe_exc(e, 260)}")
    dt = time.monotonic() - t0
    if check:
        problem = check(out)
        if problem:
            return Result(label, False, dt, problem)
    return Result(label, True, dt, str(out)[:70].replace("\n", " "))


def _parses(text: str) -> Optional[str]:
    return None if parse_interpretation(text) else f"answered, but NOT in the JSON format the assistant needs: {text[:90]!r}"


async def check_provider(p: Any, embed_dims: int = 768) -> ProviderReport:
    rep = ProviderReport(p.name, getattr(p, "model", "?"))
    rep.results.append(await _timed("tiny reply", p.generate(system="Reply with one short sentence.", prompt="Say hello to a customer.", json_mode=False, max_tokens=400, temperature=0.2)))
    rep.results.append(await _timed("realistic turn", p.generate(system=SYSTEM, prompt=PROMPT, json_mode=True, max_tokens=900, temperature=0.4), check=_parses))
    emb = await _timed("embedding", p.embed("hello", embed_dims))
    if "not supported" not in emb.detail:
        rep.results.append(emb)
    return rep


def recommend(reports: list[ProviderReport]) -> str:
    good = sorted((r for r in reports if r.healthy and r.turn_seconds is not None), key=lambda r: r.turn_seconds)
    if not good:
        return "No provider passed the realistic turn. Check the keys, quotas and model names above before going further."
    order = ",".join(r.name for r in good)
    fastest = good[0]
    lines = [f"Recommended first: {fastest.name} ({fastest.model}), realistic turn in {fastest.turn_seconds:.1f}s.", f"  LLM_PROVIDER_ORDER={order}"]
    for r in reports:
        if r.embedding_problem:
            lines.append(f"⚠ {r.name} EMBEDDINGS ARE FAILING (model {r.model} / EMBEDDING_MODEL): {r.embedding_problem}\n"
                         "  Knowledge and FAQ lookups are down. Set EMBEDDING_MODEL=gemini-embedding-2, redeploy, then POST /v1/platform/embeddings/reindex.")
    bad = [r for r in reports if not r.healthy]
    if bad:
        lines.append("Not healthy right now: " + ", ".join(f"{r.name} ({r.model})" for r in bad) + ". Leave them as a fallback only if they recover.")
    return "\n".join(lines)


def print_report(rep: ProviderReport) -> None:
    print(f"\n{rep.name}  model={rep.model}")
    for r in rep.results:
        mark = "OK    " if r.ok else "FAILED"
        if r.label == "embedding" and not r.ok:
            mark = "BROKEN"
        print(f"  {r.label:<15} {mark} {r.seconds:5.1f}s  {r.detail}")


async def list_gemini_models(api_key: str) -> None:
    try:
        from google import genai
        print("\nGemini models your key can call for chat:")
        n = 0
        async for m in await genai.Client(api_key=api_key).aio.models.list():
            if "generateContent" in (getattr(m, "supported_actions", None) or []) and "gemini" in (m.name or ""):
                print("  ", m.name.replace("models/", ""))
                n += 1
        if not n:
            print("   (none listed)")
    except Exception as e:
        print(f"  could not list models: {describe_exc(e)}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-models", action="store_true")
    ap.add_argument("--gemini-model", action="append", default=[])
    a = ap.parse_args()
    s = get_settings()
    providers: list[Any] = []
    if s.gemini_api_key:
        for m in (a.gemini_model or [s.gemini_model]):
            providers.append(GeminiProvider(s.gemini_api_key, m, s.embedding_model))
    if s.groq_api_key:
        providers.append(OpenAICompatProvider("groq", "https://api.groq.com/openai/v1", s.groq_api_key, s.groq_model, reasoning_effort=s.groq_reasoning_effort.strip()))
        if not s.groq_reasoning_effort.strip():
            low = OpenAICompatProvider("groq", "https://api.groq.com/openai/v1", s.groq_api_key, s.groq_model, reasoning_effort="low")
            low.model = f"{s.groq_model} + reasoning_effort=low"
            providers.append(low)
    if not providers:
        print("No GEMINI_API_KEY / GROQ_API_KEY found in .env")
        return
    if a.list_models and s.gemini_api_key:
        await list_gemini_models(s.gemini_api_key)
    reports = []
    for p in providers:
        rep = await check_provider(p, s.embedding_dims)
        print_report(rep)
        reports.append(rep)
    print("\n" + recommend(reports))
    if any("reasoning_effort=low" in r.model and r.healthy for r in reports):
        print("  GROQ_REASONING_EFFORT=low        <- the 'low' variant worked; set this too")


if __name__ == "__main__":
    asyncio.run(main())

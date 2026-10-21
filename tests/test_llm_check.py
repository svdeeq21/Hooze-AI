"""The AI health check must tell a slow-but-working provider from a fast-but-useless one, and recommend an order from MEASUREMENTS."""
import asyncio

from scripts import llm_check as lc

GOOD = '{"reply": "FIFA 26 on PS5 is ₦68,000. Want to pay now?", "actions": [], "intent": "buying"}'


class P:
    def __init__(self, name, model, turn=GOOD, delay=0.0, err=None, embeds=True):
        self.name, self.model, self.turn, self.delay, self.err, self.embeds = name, model, turn, delay, err, embeds
    async def generate(self, *, system, prompt, json_mode, max_tokens, temperature):
        await asyncio.sleep(self.delay)
        if self.err: raise self.err
        return self.turn if json_mode else "Hello!"
    async def embed(self, text, dims):
        if not self.embeds: raise NotImplementedError
        return [0.0] * dims


async def test_a_working_provider_is_healthy_and_recommended():
    rep = await lc.check_provider(P("groq", "m", embeds=False))
    assert rep.healthy and rep.turn_seconds is not None and [r.label for r in rep.results] == ["tiny reply", "realistic turn"]


async def test_a_provider_that_answers_but_not_in_the_required_json_is_not_healthy():
    rep = await lc.check_provider(P("groq", "m", turn="Sure! FIFA 26 is ₦68,000."))
    bad = next(r for r in rep.results if r.label == "realistic turn")
    assert not rep.healthy and "NOT in the JSON format" in bad.detail


async def test_errors_are_shown_with_their_real_message_even_when_empty():
    rep = await lc.check_provider(P("gemini", "g", err=TimeoutError()))
    assert not rep.healthy and "TimeoutError" in rep.results[0].detail


async def test_the_recommendation_orders_healthy_providers_fastest_first_and_names_the_unhealthy():
    fast = await lc.check_provider(P("groq", "gpt-oss", delay=0.01, embeds=False))
    slow = await lc.check_provider(P("gemini", "g", delay=0.05))
    down = await lc.check_provider(P("other", "o", err=RuntimeError("503 UNAVAILABLE")))
    text = lc.recommend([slow, fast, down])
    assert "LLM_PROVIDER_ORDER=groq,gemini" in text and "Not healthy right now: other" in text


async def test_with_nothing_healthy_it_says_so_instead_of_recommending():
    down = await lc.check_provider(P("gemini", "g", err=RuntimeError("503")))
    assert "No provider passed" in lc.recommend([down]) and "LLM_PROVIDER_ORDER" not in lc.recommend([down])

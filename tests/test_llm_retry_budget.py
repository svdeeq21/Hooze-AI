"""When the first model's answer is cut off or unusable, the retry must get MORE room, not the same budget that just failed."""
from hooze.llm.gateway import INTERPRET_RETRY_TOKENS, INTERPRET_TOKENS, LLMGateway

GOOD = '{"reply": "Hello there", "actions": []}'
CUT_OFF = '{"reply": "Hello th'                                          # truncated JSON: what a model that ran out of tokens returns


class Spy:
    def __init__(self, name, answer):
        self.name, self.model, self.answer, self.budgets = name, "m", answer, []
    async def generate(self, *, system, prompt, json_mode, max_tokens, temperature):
        self.budgets.append(max_tokens)
        return self.answer


async def test_the_retry_after_a_cut_off_answer_gets_a_bigger_token_budget():
    a, b = Spy("a", CUT_OFF), Spy("b", GOOD)
    res = await LLMGateway([a, b], timeout=5, provider_cooldown=0).interpret("t", system="s", prompt="p")
    assert not res.degraded and res.provider == "b"
    assert a.budgets == [INTERPRET_TOKENS] and b.budgets == [INTERPRET_RETRY_TOKENS] and INTERPRET_RETRY_TOKENS > INTERPRET_TOKENS


async def test_a_good_first_answer_does_not_pay_for_a_retry():
    a, b = Spy("a", GOOD), Spy("b", GOOD)
    await LLMGateway([a, b], timeout=5, provider_cooldown=0).interpret("t", system="s", prompt="p")
    assert a.budgets == [INTERPRET_TOKENS] and b.budgets == []

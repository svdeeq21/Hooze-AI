from tests.conftest import CUSTOMER


async def test_worlds_activate_and_a_message_round_trips(re_world, food_world):
    for w in (re_world, food_world):
        w.llm.say("Hello! What are you looking for?", intent="greeting")
        out = await w.send("hi")
        assert out.status == "replied", out
        assert w.replies() and "Hello" in w.replies()[-1]

"""Load simulator: runs the REAL engine (webhook queue -> turn pipeline -> replies) under burst scenarios, with realistic delays injected
for the database, the AI and WhatsApp. Nothing external is called. Use it to see how Hooze behaves before real customers find out.

    python -m scripts.load_sim                     # defaults below
    python -m scripts.load_sim --db-ms 120         # what a server and database in DIFFERENT regions looks like

What is real: the queue, the concurrency limit, per-customer locking, every database read/write the code makes, prompts, guards.
What is simulated: each database call takes --db-ms; an AI reply takes --llm-s; a knowledge lookup (embedding) --embed-s; WhatsApp holds
each message for the "typing…" delay (1.0-2.5 s, as configured) plus 0.15 s. The fake database has no capacity limits, so real Postgres
under heavy load would be somewhat slower than shown.
"""
from __future__ import annotations
import argparse, asyncio, statistics, time
from collections import Counter

from hooze.runtime.inbox import Inbox
from hooze.settings import Settings
from tests import fakes
from tests.conftest import food_config, make_world, realestate_config
from tests.fakes import Clock, FakeDB
from tests.test_api import HybridFactory

CALLS: Counter = Counter()
PROD_SINKS = True


def instrument(db_ms: float, llm_s: float, embed_s: float):
    """Add latency to every fake database call / AI call / WhatsApp send, and count database calls by table."""
    q_exec, rpc_exec = fakes.FakeQuery.execute, fakes.FakeRPC.execute

    async def q(self):
        CALLS[f"{self.t}.{self._op}"] += 1
        await asyncio.sleep(db_ms / 1000)
        return await q_exec(self)

    async def r(self):
        CALLS[f"rpc.{self.name}"] += 1
        await asyncio.sleep(db_ms / 1000)
        return await rpc_exec(self)
    fakes.FakeQuery.execute, fakes.FakeRPC.execute = q, r

    i_orig, e_orig, s_orig = fakes.FakeLLM.interpret, fakes.FakeLLM.embed, fakes.FakeLLM.summarize

    async def interpret(self, tenant_id, *, system, prompt):
        await asyncio.sleep(llm_s)
        getattr(self, "sizes", []).append(len(system) + len(prompt))
        return await i_orig(self, tenant_id, system=system, prompt=prompt)

    async def embed(self, tenant_id, text, **kw):
        await asyncio.sleep(embed_s)
        return await e_orig(self, tenant_id, text, **kw)

    async def summarize(self, tenant_id, transcript):
        await asyncio.sleep(llm_s * 0.6)
        return await s_orig(self, tenant_id, transcript)
    fakes.FakeLLM.interpret, fakes.FakeLLM.embed, fakes.FakeLLM.summarize = interpret, embed, summarize

    send_orig = fakes.FakeAdapter.send_text

    async def send_text(self, to, text, *, typing_delay=0.0):
        await asyncio.sleep(typing_delay + 0.15)                  # Evolution shows "typing…" for the delay, then sends
        if not hasattr(self, "sent_at"):
            self.sent_at = {}
        self.sent_at.setdefault(to, []).append(time.perf_counter())
        return await send_orig(self, to, text, typing_delay=typing_delay)
    fakes.FakeAdapter.send_text = send_text


def body(phone: str, text: str, msg_id: str, instance: str) -> dict:
    return {"event": "messages.upsert", "instance": instance, "data": {"key": {"remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False, "id": msg_id},
                                                                      "pushName": "Load Test", "messageType": "conversation", "message": {"conversation": text}}}


async def world_set(n_clients: int, concurrency: int):
    clock, db = Clock(), FakeDB(Clock())
    db.clock = clock
    worlds = []
    for i in range(n_clients):
        cfg = realestate_config() if i % 2 == 0 else food_config()
        worlds.append(await make_world(cfg, tenant_name=f"client-{i}", db=db, clock=clock))
    w0 = worlds[0]
    w0.llm.sizes = []
    w0.adapter.sent_at = {}
    from hooze.kernel.events import BufferedInserter, buffered_db_sink
    w0.configs.ttl = 30                                       # production caches each client's settings for 30 s
    if PROD_SINKS:                                            # what build_container wires in production: batched log + usage writes
        ev, us = BufferedInserter(w0.pdb, "events"), BufferedInserter(w0.pdb, "usage_events")
        w0.events.sinks.append(buffered_db_sink(ev))
        orig = w0.llm.interpret
        async def metered(tenant_id, *, system, prompt):
            out = await orig(tenant_id, system=system, prompt=prompt)
            us.add({"tenant_id": tenant_id, "kind": "llm_call", "quantity": 1, "metadata": {}})
            return out
        w0.llm.interpret = metered
    for w in worlds:
        w.settings.typing_delay_min, w.settings.typing_delay_max = 1.0, 2.5
    settings = Settings(_env_file=None, inbound_max_concurrency=concurrency)
    inbox = Inbox(w0.pdb, w0.pipeline, HybridFactory(w0.adapter), w0.events, settings, clock)
    return worlds, inbox


async def deliver(inbox, w, phone: str, text: str, msg_id: str, t0s: dict):
    """What the webhook does: save to the queue, acknowledge, process in the background."""
    t0s[msg_id] = (phone, time.perf_counter())
    event_id, new = await inbox.enqueue(w.channel, body(phone, text, msg_id, w.channel["instance_name"]), msg_id, phone=phone)
    return asyncio.create_task(inbox.process(event_id))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else 0.0


async def scenario_burst(n_clients: int, per_client: int, concurrency: int) -> dict:
    CALLS.clear()
    worlds, inbox = await world_set(n_clients, concurrency)
    w0 = worlds[0]
    t0s: dict = {}
    start = time.perf_counter()
    tasks = []
    for ci, w in enumerate(worlds):
        for k in range(per_client):
            tasks.append(await deliver(inbox, w, f"23480{ci:02d}{k:06d}", "Hi, how much is a 2 bedroom flat?", f"m-{ci}-{k}", t0s))
    results = await asyncio.gather(*tasks)
    total = time.perf_counter() - start
    waits = []
    for mid, (phone, t0) in t0s.items():
        sent = w0.adapter.sent_at.get(phone, [])
        if sent:
            waits.append(sent[0] - t0)
    turns = len(t0s)
    db_calls = sum(v for k, v in CALLS.items() if not k.startswith("inbound_events") and not k.startswith("rpc.claim"))
    return {"messages": turns, "answered": len(waits), "results": Counter(results), "total_s": total, "p50": pct(waits, 50), "p95": pct(waits, 95),
            "max": max(waits) if waits else 0, "db_per_turn": db_calls / max(1, turns), "top": CALLS.most_common(6)}


async def scenario_one_customer_burst(concurrency: int) -> dict:
    CALLS.clear()
    worlds, inbox = await world_set(1, concurrency)
    w0 = worlds[0]
    t0s: dict = {}
    tasks = []
    for k, text in enumerate(["Hi", "I'm looking for a house", "in Maitama", "budget around 150 million", "for my family"]):
        tasks.append(await deliver(inbox, w0, "2348099000001", text, f"b-{k}", t0s))
        await asyncio.sleep(0.7)
    await asyncio.gather(*tasks)
    return {"customer_messages": 5, "assistant_replies": len(w0.adapter.sent_at.get("2348099000001", [])), "ai_calls": len(w0.llm.sizes)}


async def scenario_long_conversation(n: int) -> dict:
    w = await make_world(realestate_config(stuck_after_user_messages=100_000))    # this test is about length, not about hand-offs
    w.llm.sizes = []
    checkpoints, sizes = {10, 50, 100, 200, n}, {}
    for i in range(1, n + 1):
        w.clock.advance(minutes=10)                  # a realistic pace; also stays under the per-minute and per-day reply limits
        await w.send(f"message number {i} about flats in Jabi with a budget of {i} million")
        if i in checkpoints and w.llm.calls:
            c = w.llm.calls[-1]
            sizes[i] = len(c["system"]) + len(c["prompt"])
    return {"prompt_chars": sizes, "summaries": len(w.llm.summaries), "ai_replies": len([m for m in w.db.rows("messages") if m["sender_type"] == "bot"])}


async def scenario_big_menu(items: int) -> int:
    from hooze.config_engine.schema import Offering
    bc = food_config()
    bc.offerings = [Offering(name=f"Dish {i}", description="Rice with stew, plantain and a protein of your choice", price_minor=3500_00 + i * 100) for i in range(items)]
    w = await make_world(bc)
    await w.send("what do you have?")
    c = w.llm.calls[-1]
    return len(c["system"]) + len(c["prompt"])


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-ms", type=float, default=30, help="time per database call (same region: 10-40 ms; different regions: 100-200 ms)")
    ap.add_argument("--llm-s", type=float, default=1.8, help="time per AI reply")
    ap.add_argument("--embed-s", type=float, default=0.3, help="time per knowledge lookup")
    ap.add_argument("--concurrency", type=int, default=Settings(_env_file=None).inbound_max_concurrency)
    ap.add_argument("--only", default="ABCDE", help="which scenarios to run, e.g. --only AD")
    a = ap.parse_args()
    instrument(a.db_ms, a.llm_s, a.embed_s)
    print(f"\nSimulated: database {a.db_ms:.0f} ms/call, AI reply {a.llm_s} s, lookup {a.embed_s} s, typing 1.0-2.5 s; {a.concurrency} chats processed at once\n")

    if "A" in a.only:
      r = await scenario_burst(10, 10, a.concurrency)
      print(f"A) 10 clients x 10 customers, all at the same moment ({r['messages']} messages)")
      print(f"   answered {r['answered']}/{r['messages']}  outcomes {dict(r['results'])}")
      print(f"   all done in {r['total_s']:.1f} s | first reply: median {r['p50']:.1f} s, 95% within {r['p95']:.1f} s, slowest {r['max']:.1f} s")
      print(f"   database calls per message: {r['db_per_turn']:.1f}  (busiest: {', '.join(f'{k} x{v}' for k, v in r['top'])})\n")

    if "B" in a.only:
      r = await scenario_burst(1, 5, a.concurrency)
      print(f"B) 5 customers message ONE client at the same moment")
      print(f"   answered {r['answered']}/5 | first reply: median {r['p50']:.1f} s, slowest {r['max']:.1f} s\n")

    if "C" in a.only:
      r = await scenario_one_customer_burst(a.concurrency)
      print(f"C) ONE customer sends 5 quick messages (0.7 s apart): \"Hi\" / \"I'm looking for a house\" / \"in Maitama\" / ...")
      print(f"   assistant sent {r['assistant_replies']} replies, using {r['ai_calls']} AI calls\n")

    if "D" in a.only:
      r = await scenario_long_conversation(250)
      sizes = ", ".join(f"msg {k}: ~{v // 4:,} tokens" for k, v in sorted(r["prompt_chars"].items()))
      print(f"D) One conversation, 250 customer messages over ~2 days")
      print(f"   AI prompt size: {sizes}")
      print(f"   {r['ai_replies']} AI replies, {r['summaries']} summary updates (older messages are folded into a summary)\n")

    for n in ((20, 150) if "E" in a.only else ()):
        chars = await scenario_big_menu(n)
        print(f"E) Menu with {n} items: each AI call sends ~{chars // 4:,} tokens")
    print()


if __name__ == "__main__":
    asyncio.run(main())

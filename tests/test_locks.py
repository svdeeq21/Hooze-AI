"""The in-process per-customer lock must still exclude, and must not remember every tenant:phone forever."""
import asyncio

from hooze.kernel import locks
from hooze.kernel.locks import distributed_lock


async def test_the_lock_table_is_empty_again_once_nobody_holds_or_waits():
    for i in range(50):
        async with distributed_lock(f"t:phone{i}"):
            pass
    assert locks._local_locks == {}


async def test_it_still_excludes_concurrent_turns_for_the_same_key_and_cleans_up_after():
    inside, overlap, order = 0, 0, []
    async def turn(n):
        nonlocal inside, overlap
        async with distributed_lock("t:same"):
            inside += 1; overlap = max(overlap, inside)
            await asyncio.sleep(0.01)
            order.append(n); inside -= 1
    await asyncio.gather(*[turn(i) for i in range(6)])
    assert overlap == 1 and sorted(order) == list(range(6)) and locks._local_locks == {}


async def test_the_entry_survives_while_someone_is_waiting_and_different_keys_do_not_block_each_other():
    first_in, release = asyncio.Event(), asyncio.Event()
    async def holder():
        async with distributed_lock("t:k"):
            first_in.set(); await release.wait()
    h = asyncio.create_task(holder()); await first_in.wait()
    waiter = asyncio.create_task(distributed_lock("t:k").__aenter__())
    await asyncio.sleep(0.01)
    assert "hooze:lock:t:k" in locks._local_locks and locks._local_locks["hooze:lock:t:k"][1] == 2
    async with distributed_lock("t:other"):                                      # not blocked by t:k
        pass
    release.set(); lock = await waiter; await h
    await lock.__aexit__(None, None, None)
    assert locks._local_locks == {}


async def test_a_waiter_that_is_cancelled_gives_its_slot_back():
    first_in, release = asyncio.Event(), asyncio.Event()
    async def holder():
        async with distributed_lock("t:c"):
            first_in.set(); await release.wait()
    h = asyncio.create_task(holder()); await first_in.wait()
    w = asyncio.create_task(distributed_lock("t:c").__aenter__()); await asyncio.sleep(0.01)
    w.cancel()
    try: await w
    except asyncio.CancelledError: pass
    release.set(); await h
    assert locks._local_locks == {}

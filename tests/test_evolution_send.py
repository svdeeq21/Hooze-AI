"""Sending must never produce a duplicate WhatsApp message: only retry when the request provably did not go through."""
import httpx
import pytest

from hooze.channels import evolution
from hooze.channels.evolution import EvolutionAdapter


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_): pass
    monkeypatch.setattr("hooze.kernel.retry.asyncio.sleep", instant)


def adapter(handler):
    transport = httpx.MockTransport(handler)
    return EvolutionAdapter("http://evo.test", "k", "inst", http_factory=lambda timeout=15.0: httpx.AsyncClient(timeout=timeout, transport=transport))


async def test_a_read_timeout_is_not_retried_because_the_message_may_already_be_sent():
    n = []
    def handler(req): n.append(1); raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(httpx.ReadTimeout):
        await adapter(handler).send_text("+2348012345678", "hello")
    assert len(n) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504, 400, 401])
async def test_a_server_or_client_error_is_not_retried(status):
    n = []
    def handler(req): n.append(1); return httpx.Response(status, text="x")
    with pytest.raises(RuntimeError):
        await adapter(handler).send_text("+2348012345678", "hello")
    assert len(n) == 1


async def test_a_connection_failure_is_retried_because_nothing_was_sent():
    n = []
    def handler(req):
        n.append(1)
        if len(n) < 3: raise httpx.ConnectError("refused", request=req)
        return httpx.Response(201, json={"key": {"id": "X"}})
    await adapter(handler).send_text("+2348012345678", "hello")
    assert len(n) == 3


async def test_a_429_is_retried_then_succeeds():
    n = []
    def handler(req):
        n.append(1)
        return httpx.Response(429, text="slow down") if len(n) == 1 else httpx.Response(200, json={})
    await adapter(handler).send_media("+2348012345678", "https://x/a.jpg", "image", "cap")
    assert len(n) == 2


async def test_it_gives_up_after_three_connection_failures():
    n = []
    def handler(req): n.append(1); raise httpx.ConnectTimeout("t", request=req)
    with pytest.raises(httpx.ConnectTimeout):
        await adapter(handler).send_text("+2348012345678", "hello")
    assert len(n) == 3


async def test_delete_instance_logs_out_then_deletes_and_treats_a_missing_instance_as_already_gone():
    from hooze.channels.evolution import delete_instance
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path, req.headers.get("apikey")))
        return httpx.Response(200, json={})
    make = lambda timeout=20.0: httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))
    assert await delete_instance("http://evo.test/", "KEY", "inst-1", make) == "deleted"
    assert calls == [("DELETE", "/instance/logout/inst-1", "KEY"), ("DELETE", "/instance/delete/inst-1", "KEY")]
    gone = lambda timeout=20.0: httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(lambda r: httpx.Response(404, text="nope")))
    assert await delete_instance("http://evo.test", "KEY", "inst-1", gone) == "gone"
    boom = lambda timeout=20.0: httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(lambda r: httpx.Response(500, text="x")))
    import pytest
    with pytest.raises(RuntimeError, match="500"):
        await delete_instance("http://evo.test", "KEY", "inst-1", boom)

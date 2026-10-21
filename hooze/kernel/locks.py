"""Distributed lock + shared cache (Upstash Redis REST, in-process fallback).

Extracted from: pd + savoury `distributed_state.py` (they were byte-identical
except the key prefix — the clearest example of fork drift in the two repos).
Kept: fail-OPEN on Redis outage (a silent bot is worse than a rare double
process; DB unique constraints on provider_message_id are the real backstop).
Changed: keys are namespaced by the caller with the tenant id, so one tenant's
lock/cache can never collide with another's.
"""
from __future__ import annotations
import asyncio, time, uuid
from typing import Optional
import httpx

from hooze.settings import get_settings

_local_locks: dict[str, asyncio.Lock] = {}
_local_cache: dict[str, tuple[float, str]] = {}


def _redis_cfg() -> tuple[str, str] | None:
    s = get_settings()
    return (s.upstash_redis_rest_url.rstrip("/"), s.upstash_redis_rest_token) if s.upstash_redis_rest_url and s.upstash_redis_rest_token else None


async def _redis(*args):
    cfg = _redis_cfg()
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.post(cfg[0], headers={"Authorization": f"Bearer {cfg[1]}"}, json=list(args))
        r.raise_for_status()
        return r.json().get("result")


class distributed_lock:
    def __init__(self, key: str, ttl_seconds: float = 60.0, max_wait_seconds: float = 20.0):
        self.key, self.ttl_ms, self.max_wait = f"hooze:lock:{key}", int(ttl_seconds * 1000), max_wait_seconds
        self.token, self._via_redis, self._local = uuid.uuid4().hex, False, None

    async def __aenter__(self):
        if _redis_cfg() is None:
            self._local = _local_locks.setdefault(self.key, asyncio.Lock())
            await self._local.acquire()
            return self
        deadline = time.monotonic() + self.max_wait
        while time.monotonic() < deadline:
            try:
                if await _redis("SET", self.key, self.token, "NX", "PX", str(self.ttl_ms)) == "OK":
                    self._via_redis = True
                    return self
            except Exception:
                return self  # fail open
            await asyncio.sleep(0.15)
        return self  # timeout: proceed; DB constraints backstop

    async def __aexit__(self, *exc):
        if self._local is not None:
            self._local.release()
        elif self._via_redis:
            try:
                await _redis("EVAL", "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", "1", self.key, self.token)
            except Exception:
                pass


_local_held: set[str] = set()


class try_lock:
    """NON-blocking mutual exclusion for jobs that must not overlap (the cron tick). `acquired` is False if another run holds it.

    Unlike `distributed_lock` this never waits and never "times out and proceeds": for a nudge/expiry sweep, running twice
    is the failure we are preventing (2026-08-13: one lead got ~20 follow-ups). Redis-unreachable still fails OPEN, consistent
    with the rest of the platform (a silent bot is worse), and every send path has its own reserve-before-send guard too.
    """
    def __init__(self, key: str, ttl_seconds: float = 600.0):
        self.key, self.ttl_ms, self.token = f"hooze:lock:{key}", int(ttl_seconds * 1000), uuid.uuid4().hex
        self.acquired, self._mode = False, None

    async def __aenter__(self):
        if _redis_cfg() is None:
            if self.key not in _local_held:
                _local_held.add(self.key)
                self.acquired, self._mode = True, "local"
            return self
        try:
            ok = await _redis("SET", self.key, self.token, "NX", "PX", str(self.ttl_ms)) == "OK"
        except Exception:
            self.acquired = True                          # fail open
            return self
        self.acquired, self._mode = ok, ("redis" if ok else None)
        return self

    async def __aexit__(self, *exc):
        if self._mode == "local":
            _local_held.discard(self.key)
        elif self._mode == "redis":
            try:
                await _redis("EVAL", "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", "1", self.key, self.token)
            except Exception:
                pass


async def cache_get(key: str) -> Optional[str]:
    k = f"hooze:cache:{key}"
    if _redis_cfg() is None:
        hit = _local_cache.get(k)
        if not hit or time.time() > hit[0]:
            _local_cache.pop(k, None)
            return None
        return hit[1]
    try:
        return await _redis("GET", k)
    except Exception:
        return None


async def cache_set(key: str, value: str, ttl_seconds: float) -> None:
    k = f"hooze:cache:{key}"
    if _redis_cfg() is None:
        _local_cache[k] = (time.time() + ttl_seconds, value)
        return
    try:
        await _redis("SET", k, value, "EX", str(int(ttl_seconds)))
    except Exception:
        pass


async def cache_delete(key: str) -> None:
    k = f"hooze:cache:{key}"
    if _redis_cfg() is None:
        _local_cache.pop(k, None)
        return
    try:
        await _redis("DEL", k)
    except Exception:
        pass

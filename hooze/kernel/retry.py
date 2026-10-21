"""Retry decorator.

RULE (learned from the 2026-08-13 incident where a follow-up was sent ~20x):
only ever decorate the *pure network call*. Anything that runs after a
confirmed success (logging, DB writes, state updates) must live OUTSIDE the
retried function, otherwise a late exception re-runs the send.
Both old repos had this rule written in comments in several places; here it is
enforced by structure: channel adapters expose `_post()` (retried) and keep
bookkeeping in the pipeline.
"""
from __future__ import annotations
import asyncio, functools, logging
from typing import Callable

_log = logging.getLogger("hooze.retry")


def with_retry(max_attempts: int = 3, base_delay: float = 1.0, exceptions: tuple = (Exception,)):
    def deco(fn: Callable):
        @functools.wraps(fn)
        async def wrapper(*a, **k):
            last: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return await fn(*a, **k)
                except exceptions as e:
                    last = e
                    _log.warning("retry %s attempt=%s/%s err=%s", fn.__name__, attempt + 1, max_attempts, str(e)[:160])
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(base_delay * (2 ** attempt))
            raise last  # type: ignore[misc]
        return wrapper
    return deco

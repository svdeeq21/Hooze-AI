"""Per-request capture for the web test chat and demo links.

While a browser chat turn runs, everything that would leave the building is collected here instead: the assistant's messages to
the (web) customer, alerts that would go to the owner's WhatsApp, and the turn's events (for the "what Hooze did" panel).
A context variable keeps it to the one request that set it: a real WhatsApp turn running at the same time is never affected.
"""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_CAPTURE: ContextVar[Optional[dict]] = ContextVar("hooze_capture", default=None)


def active() -> Optional[dict]:
    return _CAPTURE.get()


@contextmanager
def capturing() -> Iterator[dict]:
    box: dict = {"sent": [], "staff": [], "events": []}
    token = _CAPTURE.set(box)
    try:
        yield box
    finally:
        _CAPTURE.reset(token)

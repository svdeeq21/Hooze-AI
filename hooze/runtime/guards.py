"""Deterministic reply guards — the last line before a message reaches a customer.

Extracted from: pd `llm._looks_like_invalid_listing_leak`, `_is_too_similar`,
`message_pipeline._reply_claims_listing_sent`; savoury `_is_duplicate_bot_message`.
Generic here; capability-specific claim checks live in each capability's
`check_reply()` (e.g. ordering: "added to your cart" requires a successful add).
"""
from __future__ import annotations
import re

_LEAK = [
    r"^\s*json\s*$", r"^\s*\[\s*\{", r"^\s*\{\s*\"", r"```", r"\"[a-z_]+_(id|url)\"\s*:",
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",  # internal ids
    r"^\s*##\s", r"\bfunction\"?\s*:\s*\"",
]


def looks_like_leak(text: str) -> bool:
    t = (text or "").strip()
    return (not t) or any(re.search(p, t, re.I | re.M) for p in _LEAK)


def too_similar(a: str, b: str, threshold: float = 0.70) -> bool:
    wa, wb = set((a or "").lower().split()), set((b or "").lower().split())
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) > threshold


def is_repeat_of_last_bot(history: list[dict], candidate: str) -> bool:
    last = next((m for m in reversed(history) if m.get("sender_type") == "bot"), None)
    return bool(last) and (last.get("content") or "").strip() == (candidate or "").strip()


def strip_sentences(text: str, patterns: list[str]) -> str:
    """Drop sentences that match any pattern (used to remove unearned 'I've sent it' claims)."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [p for p in parts if not any(re.search(pat, p, re.I) for pat in patterns)]
    return " ".join(kept).strip()

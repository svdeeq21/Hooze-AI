"""WhatsApp markdown normalisation (ported verbatim from savoury `whatsapp_format.py`).

Applied at the ONE universal send boundary (Messenger), so every outbound
message — LLM text, deterministic override, hand-written Python — is
normalised, instead of hoping each call site (or the LLM) remembers to.
"""
import re


def to_whatsapp_markdown(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r"\1: \2", text)
    return text

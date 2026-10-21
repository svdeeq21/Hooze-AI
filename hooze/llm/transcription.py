"""Voice-note transcription via Groq Whisper.

Ported from pd `transcription.py`. Kept: language is DETECTED (not forced to English) and a non-English note is rejected
rather than guessed at (Whisper on Hausa/Yoruba produces confident nonsense); known Whisper hallucinations on silence
("thanks for watching") are treated as failure. Returns None on ANY failure — the pipeline then asks the customer to type
it. The audio download lives on the channel adapter (`download_media`), so this class is provider-agnostic.
Known limitation: English only. Adding Hausa/Yoruba means a different model, not a different pipeline.
"""
from __future__ import annotations
from typing import Optional

import httpx

_EMPTY = {"", "you", "thanks for watching", "thank you", "thank you.", "bye", "bye.", "thanks for watching!"}


class GroqWhisper:
    def __init__(self, api_key: str, model: str = "whisper-large-v3-turbo", allowed_languages: tuple[str, ...] = ("english", "en"), http_factory=None):
        self.api_key, self.model, self.allowed = api_key, model, {l.lower() for l in allowed_languages}
        self._http = http_factory or (lambda: httpx.AsyncClient(timeout=30.0))

    async def __call__(self, audio: bytes) -> Optional[str]:
        if not audio or not self.api_key:
            return None
        try:
            async with self._http() as c:
                r = await c.post("https://api.groq.com/openai/v1/audio/transcriptions", headers={"Authorization": f"Bearer {self.api_key}"},
                                 files={"file": ("voice.ogg", audio, "audio/ogg; codecs=opus")}, data={"model": self.model, "response_format": "verbose_json"})
            if r.status_code != 200:
                return None
            body = r.json()
            text = (body.get("text") or "").strip()
            if text.lower() in _EMPTY or len(text) < 3:
                return None
            lang = (body.get("language") or "").lower()
            return None if lang and lang not in self.allowed else text
        except Exception:
            return None

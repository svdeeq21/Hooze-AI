import httpx
import pytest

from hooze.llm.transcription import GroqWhisper


def whisper(status=200, body=None, boom=False):
    def handler(request: httpx.Request):
        if boom: raise httpx.ConnectError("down")
        return httpx.Response(status, json=body or {})
    return GroqWhisper("k", http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_english_transcript_is_returned():
    assert await whisper(body={"text": " I want a 3 bedroom in Maitama ", "language": "english"})(b"audio") == "I want a 3 bedroom in Maitama"


@pytest.mark.parametrize("body", [{"text": "Thanks for watching", "language": "english"}, {"text": "you", "language": "english"}, {"text": "ok", "language": "english"},
                                  {"text": "Ina son gida mai daki uku", "language": "hausa"}, {"text": ""}])
async def test_hallucinations_short_and_non_english_notes_are_rejected_not_guessed(body):
    assert await whisper(body=body)(b"audio") is None


async def test_every_failure_mode_returns_none_and_never_raises():
    assert await whisper(status=500)(b"audio") is None
    assert await whisper(boom=True)(b"audio") is None
    assert await whisper()(b"") is None and await GroqWhisper("")(b"audio") is None

"""The web channel behind the test chat and demo links.

It looks like any other channel to the pipeline, but nothing it "sends" leaves the server: during a browser turn the messages are
collected (kernel/capture.py) and returned to the browser; outside one (e.g. a person confirms a booking that was requested in a
test chat) they are simply recorded in the transcript by the caller, which is where the test chat reads them back from.
It never accepts webhooks: there is no WhatsApp number behind it.
"""
from __future__ import annotations
from typing import Optional

from hooze.channels.base import InboundMessage
from hooze.kernel import capture


class WebDemoAdapter:
    kind = "web_demo"

    def verify(self, *, provided_secret: Optional[str], body: dict, channel: dict) -> bool:
        return False

    def parse(self, body: dict) -> Optional[InboundMessage]:
        return None

    async def send_text(self, to: str, text: str, *, typing_delay: float = 0.0) -> None:
        cap = capture.active()
        if cap is not None:
            cap["sent"].append({"to": to, "type": "text", "text": text})

    async def send_media(self, to: str, url: str, media_type: str, caption: str = "") -> None:
        cap = capture.active()
        if cap is not None:
            cap["sent"].append({"to": to, "type": media_type or "image", "url": url, "text": caption})

    async def download_media(self, raw_message: dict) -> Optional[bytes]:
        return None

    async def connection_status(self) -> str:
        return "CONNECTED"

    async def fetch_qr(self) -> dict:
        return {"connected": True, "qr_base64": None, "pairing_code": None}

    async def set_webhook(self, url: str, headers: dict) -> None:
        return None

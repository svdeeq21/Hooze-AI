"""Evolution API adapter.

Extracted from: pd + savoury `whatsapp.py` (send paths, H3 retry boundary,
typing delay), pd `escalation.MEDIA_MESSAGE_TYPES` (media type detection),
savoury `webhook._extract_message_text` (button/list replies feed the same
text pipeline), pd `transcription.download_audio_from_evolution` (media download).

Changes:
  * `verify()` FAILS CLOSED (audit C1). Requires (a) body.instance == the
    channel's instance name and (b) a per-channel secret, compared in constant
    time against its stored hash. The secret is sent as a custom webhook header
    (`x-hooze-secret`) or `?token=` — chosen because it does not depend on an
    HMAC header format nobody confirmed (the reason pd left C1 unfixed).
  * Only `_post()` is retried; nothing after a confirmed send is inside it.
"""
from __future__ import annotations
import base64
from typing import Optional

import httpx

from hooze.channels.base import InboundMessage
from hooze.channels.formatting import to_whatsapp_markdown
from hooze.kernel.retry import with_retry
from hooze.security import secrets_match

HANDLED_EVENTS = {"messages.upsert", "MESSAGES_UPSERT", "messages_upsert"}

_KIND_BY_KEY = {
    "audioMessage": "audio", "pttMessage": "audio", "imageMessage": "image", "videoMessage": "video",
    "documentMessage": "document", "stickerMessage": "sticker", "reactionMessage": "reaction",
    "locationMessage": "location", "liveLocationMessage": "location",
    "contactMessage": "contact", "contactsArrayMessage": "contact",
}


def _extract_text(message: dict) -> str:
    """Plain text first, then interactive replies (a tap == the same words typed)."""
    if "conversation" in message:
        return message["conversation"] or ""
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text", "") or ""
    if "buttonsResponseMessage" in message:
        r = message["buttonsResponseMessage"]
        return r.get("selectedDisplayText") or r.get("selectedButtonId", "")
    if "listResponseMessage" in message:
        r = message["listResponseMessage"]
        return r.get("title") or (r.get("singleSelectReply") or {}).get("selectedRowId", "")
    if "templateButtonReplyMessage" in message:
        r = message["templateButtonReplyMessage"]
        return r.get("selectedDisplayText") or r.get("selectedId", "")
    return ""


def _phone_from_jid(jid: str) -> str:
    return "".join(ch for ch in jid.split("@")[0] if ch.isdigit())


class _RateLimited(RuntimeError):
    """HTTP 429 from Evolution: the request was refused, nothing was sent, so retrying is safe."""


class EvolutionAdapter:
    kind = "whatsapp_evolution"

    def __init__(self, base_url: str, api_key: str, instance_name: str, http_factory=None):
        self.base_url, self.api_key, self.instance = base_url.rstrip("/"), api_key, instance_name
        self._http = http_factory or (lambda timeout=15.0: httpx.AsyncClient(timeout=timeout))

    # ── inbound ───────────────────────────────────────────────────
    def verify(self, *, provided_secret: Optional[str], body: dict, channel: dict) -> bool:
        if body.get("instance") != channel.get("instance_name"):
            return False
        return secrets_match(provided_secret, channel.get("secret_hash"))

    def parse(self, body: dict) -> Optional[InboundMessage]:
        if body.get("event") not in HANDLED_EVENTS:
            return None
        data = body.get("data") or {}
        key, mtype = data.get("key") or {}, data.get("messageType")
        if not key or not mtype:
            return None
        jid = key.get("remoteJid", "")
        message = data.get("message") or {}
        text = _extract_text(message)
        kind = "text" if text else next((v for k, v in _KIND_BY_KEY.items() if k in message or k == mtype), "other")
        return InboundMessage(
            provider_message_id=key.get("id"), phone=_phone_from_jid(jid), text=text, kind=kind,
            push_name=data.get("pushName"), from_me=bool(key.get("fromMe")), is_group="@g.us" in jid,
            raw={"key": key, "message": message, "messageType": mtype},
        )

    # ── outbound ──────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {"apikey": self.api_key, "Content-Type": "application/json"}

    @with_retry(max_attempts=3, base_delay=2.0, exceptions=(httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, _RateLimited))
    async def _post(self, path: str, payload: dict, timeout: float = 15.0) -> dict:
        """The pure network call — the ONLY thing that is retried (audit H3), and only when the request PROVABLY did not go through:
        we could not connect (nothing was sent) or Evolution said "slow down" (429). A read timeout or a 5xx is AMBIGUOUS — Evolution may
        already have accepted and sent the message — so it is raised as is and never re-sent (re-sending produced duplicate WhatsApp messages).
        The inbound queue handles a failed turn separately, with its own replay guard."""
        async with self._http(timeout) as c:
            r = await c.post(f"{self.base_url}{path}/{self.instance}", json=payload, headers=self._headers())
        if r.status_code == 429:
            raise _RateLimited(f"Evolution {path} 429: {r.text[:200]}")
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Evolution {path} {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    async def send_text(self, to: str, text: str, *, typing_delay: float = 0.0) -> None:
        await self._post("/message/sendText", {
            "number": to.lstrip("+"), "text": to_whatsapp_markdown(text),
            "options": {"delay": int(typing_delay * 1000), "presence": "composing"},
        })

    async def send_media(self, to: str, url: str, media_type: str, caption: str = "") -> None:
        ext = url.lower().split("?")[0].rsplit(".", 1)[-1]
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                "mp4": "video/mp4", "pdf": "application/pdf"}.get(ext, "application/octet-stream")
        await self._post("/message/sendMedia", {
            "number": to.lstrip("+"), "mediatype": media_type, "mimetype": mime,
            "caption": to_whatsapp_markdown(caption), "media": url, "fileName": "",
        }, timeout=30.0)

    async def download_media(self, raw_message: dict) -> Optional[bytes]:
        try:
            async with self._http(30.0) as c:
                r = await c.post(f"{self.base_url}/chat/getBase64FromMediaMessage/{self.instance}",
                                 json={"message": {"key": raw_message.get("key")}, "convertToMp4": False}, headers=self._headers())
            b64 = (r.json() or {}).get("base64") if r.status_code in (200, 201) else None
            return base64.b64decode(b64) if b64 else None
        except Exception:
            return None

    async def fetch_qr(self) -> dict:
        """Pairing info for the dashboard. `qr_base64` is a data-URL-or-base64 PNG while the instance is waiting to be scanned."""
        async with self._http(15.0) as c:
            r = await c.get(f"{self.base_url}/instance/connect/{self.instance}", headers=self._headers())
        if r.status_code != 200:
            raise RuntimeError(f"Evolution connect {r.status_code}: {r.text[:200]}")
        d = r.json() or {}
        state = (d.get("instance") or {}).get("state")
        return {"connected": state == "open", "qr_base64": d.get("base64"), "pairing_code": d.get("pairingCode")}

    async def set_webhook(self, url: str, headers: dict) -> None:
        """Point this instance's MESSAGES_UPSERT events at `url`, sending `headers` (our x-hooze-secret). Evolution v2 payload shape."""
        await self._post("/webhook/set", {"webhook": {"enabled": True, "url": url, "headers": headers, "byEvents": False, "base64": False,
                                                      "events": ["MESSAGES_UPSERT"]}})

    async def connection_status(self) -> str:
        try:
            async with self._http(5.0) as c:
                r = await c.get(f"{self.base_url}/instance/connectionState/{self.instance}", headers=self._headers())
            state = (r.json().get("instance") or {}).get("state", "") if r.status_code == 200 else ""
            return {"open": "CONNECTED", "connecting": "CONNECTING"}.get(state, "DISCONNECTED" if state else "UNKNOWN")
        except Exception:
            return "UNKNOWN"


async def create_instance(base_url: str, api_key: str, instance_name: str, http_factory=None) -> dict:
    """Create a Baileys (QR-pairing) instance on an Evolution server. Raises if Evolution refuses (e.g. the name is already taken)."""
    make = http_factory or (lambda timeout=20.0: httpx.AsyncClient(timeout=timeout))
    async with make(20.0) as c:
        r = await c.post(f"{base_url.rstrip('/')}/instance/create", headers={"apikey": api_key, "Content-Type": "application/json"},
                         json={"instanceName": instance_name, "qrcode": True, "integration": "WHATSAPP-BAILEYS"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Evolution create {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def delete_instance(base_url: str, api_key: str, instance_name: str, http_factory=None) -> str:
    """Log the WhatsApp session out and delete the instance from the Evolution server. "gone" if it did not exist (already removed)."""
    make = http_factory or (lambda timeout=20.0: httpx.AsyncClient(timeout=timeout))
    base, headers = base_url.rstrip("/"), {"apikey": api_key}
    async with make(20.0) as c:
        try:
            await c.delete(f"{base}/instance/logout/{instance_name}", headers=headers)       # best effort: it may not be connected
        except Exception:
            pass
        r = await c.delete(f"{base}/instance/delete/{instance_name}", headers=headers)
    if r.status_code == 404:
        return "gone"
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Evolution delete {r.status_code}: {r.text[:200]}")
    return "deleted"

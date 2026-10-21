"""Channel abstraction.

Why this exists: both old backends hard-wire Evolution API (an *unofficial*
WhatsApp Web/Baileys bridge) into every service. That is fine for two pilot
clients and a real business risk for a SaaS: numbers get banned, interactive
messages silently break on WhatsApp updates (savoury's README documents this),
and the official Meta Cloud API has different rules (24h window, templates).
The pipeline only ever talks to this interface, so swapping/adding a provider
is one new adapter, not a rewrite.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass
class InboundMessage:
    provider_message_id: Optional[str]
    phone: str                      # digits only, e.g. 2348012345678
    text: str                       # "" for non-text kinds until preprocessed
    kind: str = "text"              # text|audio|image|video|document|sticker|reaction|location|contact|other
    push_name: Optional[str] = None
    from_me: bool = False
    is_group: bool = False
    raw: dict = field(default_factory=dict)


class ChannelAdapter(Protocol):
    kind: str

    def verify(self, *, provided_secret: Optional[str], body: dict, channel: dict) -> bool: ...
    def parse(self, body: dict) -> Optional[InboundMessage]: ...
    async def send_text(self, to: str, text: str, *, typing_delay: float = 0.0) -> None: ...
    async def send_media(self, to: str, url: str, media_type: str, caption: str = "") -> None: ...
    async def download_media(self, raw_message: dict) -> Optional[bytes]: ...
    async def connection_status(self) -> str: ...


class AdapterFactory:
    """Builds the right adapter for a `channels` row. Tests replace this with a fake."""
    def __init__(self, vault=None, http_client_factory=None):
        self.vault, self._http = vault, http_client_factory

    def for_channel(self, channel: dict) -> ChannelAdapter:
        kind = channel["kind"]
        if kind == "whatsapp_evolution":
            from hooze.channels.evolution import EvolutionAdapter
            cfg = channel.get("config") or {}
            api_key = self.vault.decrypt(channel["credentials_enc"]) if self.vault and channel.get("credentials_enc") else ""
            return EvolutionAdapter(base_url=cfg["base_url"], api_key=api_key, instance_name=channel["instance_name"], http_factory=self._http)
        raise ValueError(f"unsupported channel kind: {kind}")

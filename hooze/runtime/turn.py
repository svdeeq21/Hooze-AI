from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from hooze.channels.base import InboundMessage
from hooze.runtime.config import RuntimeConfig
from hooze.runtime.models import ToolResult


@dataclass
class Turn:
    """Everything a gate / tool / guard needs about the message currently being processed."""
    tenant: dict
    channel: dict
    contact: dict
    conversation: dict
    inbound: InboundMessage
    text: str
    config: RuntimeConfig
    db: Any                      # TenantDB
    store: Any                   # ConversationStore
    out: Any                     # Messenger
    events: Any
    llm: Any
    now: datetime
    trace_id: str
    is_new_contact: bool = False
    history: list[dict] = field(default_factory=list)   # oldest-first [{sender_type, content}]
    state: str = ""
    intent: Optional[str] = None
    results: list[ToolResult] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    greeting_sent: bool = False        # the scripted welcome already went out in THIS turn (the model must not greet again)
    scratch: dict = field(default_factory=dict)   # per-turn working memory shared by a capability's context() and tools

    @property
    def tenant_id(self) -> str:
        return self.tenant["id"]

    def tool_ok(self, name: str) -> bool:
        return any(r.tool == name and r.ok for r in self.results)

"""RuntimeConfig: the COMPILED, per-tenant configuration the pipeline executes.

Produced by config_engine.compiler from a validated BusinessConfig + a pack.
This object is the whole answer to "why is Hooze one product, not N forks":
everything that differs between Praise Dynasty and Savoury Spud is in here as
data (persona, enabled capabilities + their config, playbook), not in code.
"""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field

from hooze.runtime.playbook import PlaybookSpec


class Persona(BaseModel):
    business_name: str
    tone: str = "warm, brief and professional"
    language: str = "English"
    rules: list[str] = Field(default_factory=list)          # tenant-specific behavioural rules
    never_say: list[str] = Field(default_factory=list)      # prohibited claims / topics
    facts: dict[str, str] = Field(default_factory=dict)     # OFFICIAL facts: links, address, hours (the model must not invent these)


class RuntimeConfig(BaseModel):
    version: int = 1
    persona: Persona
    timezone: str = "Africa/Lagos"
    currency: str = "NGN"
    capabilities: dict[str, dict[str, Any]] = Field(default_factory=dict)   # name -> its config; presence == enabled
    playbook: Optional[PlaybookSpec] = None
    intents: list[str] = Field(default_factory=lambda: ["greeting", "question", "order", "booking", "objection", "human_request", "not_interested", "other"])
    staff_numbers: list[str] = Field(default_factory=list)
    history_window: int = 24
    first_contact_message: Optional[str] = None             # deterministic welcome, never LLM-improvised
    max_bot_replies_per_minute: int = 8                     # pd's check_rate_limit, now config
    media_replies: dict[str, Optional[str]] = Field(default_factory=dict)   # per-kind canned reply; None = ignore silently

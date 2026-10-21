"""Core runtime types shared by the pipeline, capabilities and the LLM gateway."""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class Action(BaseModel):
    """One thing the LLM PROPOSES. Never executed directly — see runtime/actions.py."""
    function: str
    args: dict[str, Any] = Field(default_factory=dict)


class Interpretation(BaseModel):
    """The LLM's whole output for a turn: proposed actions + a draft reply + a closed-set intent."""
    actions: list[Action] = Field(default_factory=list)
    reply: str = ""
    intent: Optional[str] = None


class ToolResult(BaseModel):
    tool: str = ""
    ok: bool = True
    # If set, this REPLACES the LLM's draft reply. Deterministic truth beats model optimism
    # (savoury's core lesson: "Adding that to your cart!" when nothing was added).
    message: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    internal_error: Optional[str] = None  # logged, never shown to the customer


class BusinessRuleError(Exception):
    """Raised by a tool for a violation the CUSTOMER must hear about (e.g. no delivery address yet).
    The executor stops the turn and sends this text verbatim instead of the LLM's reply."""

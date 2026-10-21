"""Capability contract: how a tenant's behaviour is composed instead of forked.

A capability is a self-contained module a tenant switches on via config. It can
contribute, all optionally:
  tools()          actions the LLM may PROPOSE (validated + executed deterministically)
  gate()           a deterministic pre-LLM interception (closed hours, payment pending)
  context()        facts injected into the prompt (menu+cart, matching listings, known slots)
  check_reply()    a guard on the final reply text (no "added!" without an add)
  next_step()      a deterministic "what can you do now" line
  staff_commands() commands the business owner can text (pause orders, sold out, resume)
  after_turn()     bookkeeping / invariants (e.g. qualification stuck -> human)
  on_inbound()     reset per-contact timers (nudges)
  tick()           background work driven by /internal/tick (follow-ups, expiries)

Real estate = qualification + knowledge + booking + nudges + handoff.
Food ordering = availability + ordering (+payments) + handoff.
Neither needs an `if industry ==` anywhere in the core.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, ClassVar, Optional

from hooze.runtime.actions import Tool


@dataclass
class CapabilityContext:
    tenant_id: str
    db: Any                      # TenantDB
    events: Any
    llm: Any
    outbox: Any
    config: Any                  # RuntimeConfig
    clock: Callable[[], datetime]
    extras: dict = field(default_factory=dict)   # e.g. {"payments": PaymentProvider}


@dataclass
class GateResult:
    reply: Optional[str] = None      # deterministic reply
    handled: bool = True             # True => stop; do not call the LLM
    dedupe: bool = False             # suppress if identical to the last bot message


@dataclass
class StaffCommand:
    name: str
    patterns: list[str]
    handler: Callable[[re.Match, str], Awaitable[str]]   # (match, actor_phone) -> reply text


class Capability:
    name: ClassVar[str] = ""

    def __init__(self, ctx: CapabilityContext, config: dict):
        self.ctx, self.config = ctx, config

    def tools(self) -> list[Tool]: return []
    async def gate(self, turn) -> Optional[GateResult]: return None
    async def context(self, turn) -> str: return ""
    def check_reply(self, turn, reply: str, results: list) -> Optional[str]: return None
    async def next_step(self, turn) -> Optional[str]: return None
    def staff_commands(self) -> list[StaffCommand]: return []
    async def after_turn(self, turn) -> None: return None
    async def on_inbound(self, turn) -> None: return None
    async def tick(self) -> dict: return {}


CAPABILITIES: dict[str, type[Capability]] = {}


def register(cls: type[Capability]) -> type[Capability]:
    CAPABILITIES[cls.name] = cls
    return cls


def build_capabilities(ctx: CapabilityContext, enabled: dict[str, dict]) -> dict[str, Capability]:
    import hooze.capabilities  # noqa: F401  (imports register every built-in capability)
    return {name: CAPABILITIES[name](ctx, cfg) for name, cfg in enabled.items() if name in CAPABILITIES}

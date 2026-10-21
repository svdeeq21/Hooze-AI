"""Action runtime: the LLM PROPOSES, deterministic code DISPOSES.

This is the single most valuable idea in the two codebases, and savoury learned it
from real transcripts:
  * the LLM never writes to the database and never computes a price;
  * every proposed action is looked up in a registry (hallucinated tools/products
    fail the lookup and become a logged no-op, not a write),
  * arguments are schema-validated,
  * a tool can return an AUTHORITATIVE message that replaces the LLM's draft reply
    ("Adding that to your cart now!" when nothing was added was the actual bug),
  * a tool can raise BusinessRuleError to stop the turn with a message the customer
    must hear.
pd's real-estate flow gets the same guarantees for free: listings, prices and links
come from records via `share_item`, the model only chooses *which* record.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, ValidationError

from hooze.runtime.models import Action, BusinessRuleError, ToolResult


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any, BaseModel], Awaitable[ToolResult]]
    capability: str = ""

    def signature(self) -> str:
        props = self.args_model.model_json_schema().get("properties", {})
        parts = [f'"{k}": {v.get("type", "any")}' for k, v in props.items()]
        return '{"function": "%s"%s}  -- %s' % (self.name, "".join(", " + p for p in parts), self.description)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def describe(self) -> str:
        return "\n".join(t.signature() for t in self._tools.values()) or "(no actions available)"

    def __len__(self) -> int:
        return len(self._tools)


@dataclass
class Execution:
    results: list[ToolResult]
    halted: bool = False

    def override(self) -> Optional[str]:
        """Authoritative reply text, if any tool produced one (last one wins)."""
        msgs = [r.message for r in self.results if r.message]
        return msgs[-1] if msgs else None


async def execute(turn, actions: list[Action], registry: ToolRegistry) -> Execution:
    results: list[ToolResult] = []
    for a in actions:
        tool = registry.get(a.function)
        if tool is None:
            await turn.events.warn("LLM_UNKNOWN_TOOL", tenant_id=turn.tenant_id, trace_id=turn.trace_id, tool=a.function)
            results.append(ToolResult(tool=a.function, ok=False, internal_error="unknown tool"))
            continue
        try:
            args = tool.args_model(**a.args)
        except ValidationError as e:
            await turn.events.warn("LLM_BAD_TOOL_ARGS", tenant_id=turn.tenant_id, trace_id=turn.trace_id, tool=a.function, error=str(e)[:200])
            results.append(ToolResult(tool=a.function, ok=False, internal_error="invalid arguments"))
            continue
        try:
            res = await tool.handler(turn, args)
            res.tool = res.tool or tool.name
            results.append(res)
        except BusinessRuleError as e:
            results.append(ToolResult(tool=tool.name, ok=False, message=str(e)))
            return Execution(results, halted=True)   # stop; discard the rest of a now-inconsistent action list
    return Execution(results)

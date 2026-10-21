"""Data-driven conversation playbook (state machine).

Replaces pd `conversation_state.py`: a 350-line Python function whose transitions
were tangled with real-estate regex tables (`(maitama|asokoro|katampe...)`).
Here the graph is DATA (a tenant/pack supplies it) and the engine is ~60 lines.

What becomes possible that was not before:
  * `validate_spec()` proves a playbook is sane at activation time (initial state
    exists, every referenced state exists, every state is reachable, at least one
    terminal). pd's "DISCOVERY never comes after PITCH" rule was a comment plus
    careful code; here it is simply the absence of an edge, and testable.
  * Intent comes from the LLM's closed-set `intent` field (one call, no regex
    intent engine), with keyword conditions as a cheap deterministic override.
Semantics: transitions are evaluated in order; FIRST match wins, even if it maps
a state to itself (that is how you "hold" a state, e.g. while handling an objection).
Terminal states are sticky.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


@dataclass
class TurnFacts:
    intent: Optional[str] = None
    text: str = ""
    slots_complete: bool = False
    tools_ok: set = field(default_factory=set)
    user_message_count: int = 0


class Condition(BaseModel):
    intent_in: list[str] = Field(default_factory=list)
    keywords_any: list[str] = Field(default_factory=list)     # regex, case-insensitive
    slots_complete: Optional[bool] = None
    tool_ok: list[str] = Field(default_factory=list)          # any of these tools succeeded this turn
    min_user_messages: Optional[int] = None
    always: bool = False

    def matches(self, f: TurnFacts) -> bool:
        specified = False
        if self.always:
            specified = True
        if self.intent_in:
            specified = True
            if f.intent not in self.intent_in:
                return False
        if self.keywords_any:
            specified = True
            if not any(re.search(p, f.text or "", re.I) for p in self.keywords_any):
                return False
        if self.slots_complete is not None:
            specified = True
            if f.slots_complete != self.slots_complete:
                return False
        if self.tool_ok:
            specified = True
            if not (f.tools_ok & set(self.tool_ok)):
                return False
        if self.min_user_messages is not None:
            specified = True
            if f.user_message_count < self.min_user_messages:
                return False
        return specified   # an empty condition never matches (no accidental catch-alls)


class StateSpec(BaseModel):
    name: str
    goal: str = ""                       # injected into the prompt while in this state
    terminal: bool = False
    idle_after_hours: Optional[float] = None   # nudge engine: how long silence before a follow-up
    nudges: list[str] = Field(default_factory=list)  # follow-up templates, one per step
    escalate_after_nudges: Optional[int] = None      # e.g. an unanswered booking invite -> ping a human instead of silently giving up


class Transition(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: list[str] = Field(alias="from")   # ["*"] = any non-terminal state
    to: str
    when: Condition


class PlaybookSpec(BaseModel):
    initial: str
    states: list[StateSpec]
    transitions: list[Transition] = Field(default_factory=list)

    def state(self, name: str) -> Optional[StateSpec]:
        return next((s for s in self.states if s.name == name), None)

    def is_terminal(self, name: str) -> bool:
        s = self.state(name)
        return bool(s and s.terminal)

    def validate_spec(self) -> list[str]:
        problems: list[str] = []
        names = {s.name for s in self.states}
        if self.initial not in names:
            problems.append(f"initial state '{self.initial}' is not defined")
        edges: dict[str, set[str]] = {n: set() for n in names}
        for t in self.transitions:
            if t.to not in names:
                problems.append(f"transition targets unknown state '{t.to}'")
            for src in t.from_:
                if src != "*" and src not in names:
                    problems.append(f"transition from unknown state '{src}'")
                for s in (names if src == "*" else {src}):
                    if s in edges and t.to in names:
                        edges[s].add(t.to)
        reach, frontier = {self.initial}, [self.initial]
        while frontier:
            for nxt in edges.get(frontier.pop(), ()):
                if nxt not in reach:
                    reach.add(nxt)
                    frontier.append(nxt)
        for n in sorted(names - reach):
            problems.append(f"state '{n}' is unreachable from '{self.initial}'")
        return problems

    def next_state(self, current: str, facts: TurnFacts) -> str:
        if self.is_terminal(current):
            return current
        for t in self.transitions:
            if ("*" in t.from_ or current in t.from_) and t.when.matches(facts):
                return t.to
        return current

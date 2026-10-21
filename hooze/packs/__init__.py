"""Packs = reusable SALES-PROCESS templates, selected by `sales_process.goal` — never by industry.

Adding a vertical that fits an existing goal (a dental clinic, a school, a car dealer are all
`consult_and_book`) costs ZERO code. Adding a genuinely new sales process costs one pack + at most one capability.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from hooze.runtime.playbook import PlaybookSpec


@dataclass
class Pack:
    goal: str
    playbook: PlaybookSpec
    rules: list[str]
    intents: list[str]
    capabilities: Callable   # (BusinessConfig) -> dict[str, dict]


def get_pack(goal: str) -> Pack:
    from hooze.packs import consult_and_book, catalog_order
    return {"consult_and_book": consult_and_book.PACK, "order": catalog_order.PACK}[goal]

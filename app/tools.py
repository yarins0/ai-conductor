"""Tool Registry — the open set of tools an assistant may invoke.

A tool is fulfilled by a Provider (app/providers.py). The registry is a plain
dict, not a plugin-discovery system: reach/qualify/book are registered here at
import time, and adding a tool means registering another Provider — this is
the seam that keeps the tool set open without hardcoding steps into the
Runtime.
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import LeadRecord


class ToolResult(BaseModel):
    tool: str
    status: Literal["ok", "error"]  # did the provider execute at all
    outcome: str  # business result, e.g. "answered" | "no_answer" | "qualified" | ...
    summary: str  # one human-readable line for the run log / future SSE
    data: dict[str, Any] = Field(default_factory=dict)  # tool-specific payload


class Provider(ABC):
    @abstractmethod
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult: ...


TOOL_REGISTRY: dict[str, Provider] = {}


def register_provider(name: str, provider: Provider) -> None:
    TOOL_REGISTRY[name] = provider


def get_provider(name: str) -> Provider | None:
    # None on unknown — caller handles (e.g. an "unknown_tool" outcome), never raise.
    return TOOL_REGISTRY.get(name)

"""Provider contract + Tool Registry — the foundation the concrete providers build on.

Defines what a Provider is (`Provider`, and the `CredentialGatedProvider` base for
real integrations), the typed `ToolResult` every provider returns, and the open
Tool Registry that maps a tool name to its providers. The registry is a plain dict,
not a plugin-discovery system: each service module (reach/qualify/book) registers
its providers at import, and adding a tool means adding a module — the seam that
keeps the tool set open without hardcoding steps into the Runtime.

This module depends only on `app.db`; the concrete providers depend on it. Keeping
that one-directional (base ← service modules) is what avoids an import cycle.
"""

import os
from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.db import LeadRecord


class ProviderConfigError(Exception):
    pass


class ToolResult(BaseModel):
    tool: str
    status: Literal["ok", "error"]  # did the provider execute at all
    outcome: str  # business result, e.g. "answered" | "no_answer" | "qualified" | ...
    summary: str  # one human-readable line for the run log / future SSE
    data: dict[str, Any] = Field(default_factory=dict)  # tool-specific payload


class Provider(ABC):
    # Whether this tool's work is *about* a lead. False lets the tool run with
    # lead=None — book holding plain time on the operator's own calendar. Asked
    # of the provider rather than matched on tool name in the caller, so the
    # registry stays open: a new standalone tool declares itself here.
    requires_lead: bool = True

    @abstractmethod
    async def execute(self, lead: LeadRecord | None, settings: dict[str, Any]) -> ToolResult: ...

    def check_credentials(self) -> None:
        """Preflight hook: real providers override to assert their env vars."""
        return None


class CredentialGatedProvider(Provider):
    """Base for real integration adapters. Declares the env vars the integration
    needs; check_credentials() hard-fails preflight when any is missing. execute()
    is the marked integration point — it raises until the real SDK call is wired,
    and because preflight blocks a creds-less selection, it never runs in the
    simulated demo. Subclasses only set `label` and `required_env`.
    """

    label: str = ""
    required_env: list[str] = []

    def check_credentials(self) -> None:
        for var in self.required_env:
            if not os.getenv(var):
                raise ProviderConfigError(f"{self.label} needs {var}")

    async def execute(self, lead: LeadRecord | None, settings: dict[str, Any]) -> ToolResult:
        raise NotImplementedError(f"{self.label}: wire up the real integration here.")


# Nested registry: tool -> provider_id -> Provider. One tool can be fulfilled by
# several providers (simulated + real), with one marked default per tool.
TOOL_REGISTRY: dict[str, dict[str, Provider]] = {}
DEFAULT_PROVIDER: dict[str, str] = {}
PROVIDER_LABELS: dict[str, dict[str, str]] = {}


def register_provider(tool: str, provider_id: str, provider: Provider, *, label: str, default: bool = False) -> None:
    TOOL_REGISTRY.setdefault(tool, {})[provider_id] = provider
    PROVIDER_LABELS.setdefault(tool, {})[provider_id] = label
    if default:
        DEFAULT_PROVIDER[tool] = provider_id


def get_provider(tool: str, provider_id: str | None = None) -> Provider | None:
    # None id -> the tool's default provider. Keeps get_provider("reach")
    # (single-arg) resolving to the default, which the tests rely on.
    pid = provider_id or DEFAULT_PROVIDER.get(tool)
    if pid is None:
        return None
    return TOOL_REGISTRY.get(tool, {}).get(pid)


def list_providers() -> dict[str, list[dict[str, Any]]]:
    # Payload for the session-page dropdowns.
    return {
        tool: [
            {"id": pid, "label": PROVIDER_LABELS[tool][pid], "default": DEFAULT_PROVIDER.get(tool) == pid}
            for pid in providers
        ]
        for tool, providers in TOOL_REGISTRY.items()
    }

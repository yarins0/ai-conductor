"""Assistant Spec — the config artifact the Builder writes and the Runtime reads.

The JSON Schema derived from these models (see assistant_spec_json_schema below)
is handed to the Builder LLM as-is. It is the single source of truth: never
hand-maintain a second copy of this shape in a prompt, they will drift.
"""

from typing import Any

from pydantic import BaseModel, Field


class ToolConfig(BaseModel):
    name: str  # registry key, e.g. "reach" | "qualify" | "book" — open set, not an enum
    settings: dict[str, Any] = Field(default_factory=dict)


class AssistantSpec(BaseModel):
    name: str
    objective: str
    persona: str  # voice/behavior description
    instructions: list[str] = Field(default_factory=list)
    tools: list[ToolConfig]
    # ISO-639-1 code (e.g. "en", "he"). Pins transcription and the on-call
    # prompt to one language so a mis-transcribed turn can't flip the
    # conversation into another one mid-call.
    language: str = "en"


def assistant_spec_json_schema() -> dict[str, Any]:
    return AssistantSpec.model_json_schema()

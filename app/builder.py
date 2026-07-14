"""Builder create path: natural language description -> validated AssistantSpec.

One tool-forced Anthropic call. The tool's input_schema is derived from the
Pydantic model (single schema, no hand-maintained copy), and the tool input is
validated back through the same model before anything is persisted.

Forced tool use is used instead of the structured-outputs response format
because the spec's open `settings` dict (seam: tool-specific config) is not
expressible under structured outputs' `additionalProperties: false` rule.
"""

import anthropic
from pydantic import ValidationError

from app.spec import AssistantSpec, assistant_spec_json_schema

BUILDER_MODEL = "claude-sonnet-5"
SPEC_TOOL_NAME = "save_assistant_spec"
MAX_OUTPUT_TOKENS = 4096

# The Builder's persona: turn a plain-language description into a complete
# spec, defaulting to the three registered demo tools when the user is vague.
SYSTEM_PROMPT = (
    "You are the Builder for a voice AI assistant platform. The user describes "
    "the assistant they want in plain language; you produce a complete Assistant "
    "Spec via the save_assistant_spec tool.\n"
    "- `name` is a short human-readable title.\n"
    "- `objective` is one sentence describing what the assistant achieves per lead.\n"
    "- `persona` describes voice and manner on a call.\n"
    "- `instructions` are concrete behavioral rules for the assistant.\n"
    '- `tools` reference registry keys; the available registry keys are "reach" '
    '(place an outbound call), "qualify" (score lead intent), and "book" '
    "(schedule a meeting). Include the tools the described assistant needs; "
    "include all three when the user describes a full outreach flow."
)


class BuilderError(Exception):
    """The LLM failed to produce a schema-valid spec. Message is user-facing."""


def create_spec(description: str) -> AssistantSpec:
    """Generate a spec from a description; raise BuilderError on invalid output."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=BUILDER_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        tools=[
            {
                "name": SPEC_TOOL_NAME,
                "description": "Save the completed assistant specification.",
                "input_schema": assistant_spec_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": SPEC_TOOL_NAME},
        messages=[{"role": "user", "content": description}],
    )

    tool_use_block = next(
        (block for block in response.content if block.type == "tool_use"), None
    )
    if tool_use_block is None:
        # e.g. a refusal stop reason — no spec was produced at all
        raise BuilderError(
            "The Builder did not produce a spec for that description. "
            "Try rephrasing what you want the assistant to do."
        )

    # Boundary validation (implementation rule #2): never trust LLM output.
    try:
        return AssistantSpec.model_validate(tool_use_block.input)
    except ValidationError as validation_error:
        raise BuilderError(
            "The Builder produced an invalid spec and it was not saved. "
            f"Validation errors: {validation_error}"
        ) from validation_error

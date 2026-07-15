"""LLM-facing tool definitions for the live agent loop.

These are the Anthropic tool schemas the live voice agent sees — one per
registry tool. They are deliberately hand-written (three literal dicts) rather
than derived from the providers: a provider's `execute(lead, settings)` takes an
open settings dict, so there is no per-tool argument schema to derive from. The
`input_schema` here declares the few arguments the model may author from the
conversation (e.g. a requested meeting time); those merge into `settings` at
call time, so no provider signature changes.

Only the tools present in a spec are exposed to the model (see `tools_for_spec`),
keeping the "allowed tools = the spec's tool list" seam intact.
"""

from typing import Any

from app.spec import AssistantSpec

# tool name -> Anthropic tool definition. `input_schema` args are optional and
# flow into the provider's settings dict; the simulated providers ignore them,
# real ones (e.g. Twilio's twiml_url) can read them.
_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "reach": {
        "name": "reach",
        "description": (
            "Place an outbound voice call to the lead. Use when the conversation "
            "calls for reaching the lead on the phone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "twiml_url": {
                    "type": "string",
                    "description": "Optional URL for call instructions (real telephony only).",
                }
            },
        },
    },
    "qualify": {
        "name": "qualify",
        "description": (
            "Score the lead's intent / qualification. Use when you need to judge "
            "whether the lead is a good fit before booking."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "string",
                    "description": "Optional qualification notes gathered from the conversation.",
                }
            },
        },
    },
    "book": {
        "name": "book",
        "description": (
            "Schedule a meeting with the lead. Use when the lead wants to book "
            "time or has agreed to a meeting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "preferred_time": {
                    "type": "string",
                    "description": "Optional meeting time the lead requested, in plain text.",
                }
            },
        },
    },
}


def tools_for_spec(spec: AssistantSpec) -> list[dict[str, Any]]:
    """The tool definitions for exactly the tools the spec allows, in spec order.

    A spec tool with no known definition (an unregistered future tool) is skipped
    rather than raising — the loop simply can't invoke what it can't describe.
    """
    return [_TOOL_DEFINITIONS[tool.name] for tool in spec.tools if tool.name in _TOOL_DEFINITIONS]

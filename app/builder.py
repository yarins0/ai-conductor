"""Builder create path: natural language description -> validated AssistantSpec.

One tool-forced Anthropic call. The tool's input_schema is derived from the
Pydantic model (single schema, no hand-maintained copy), and the tool input is
validated back through the same model before anything is persisted.

Forced tool use is used instead of the structured-outputs response format
because the spec's open `settings` dict (seam: tool-specific config) is not
expressible under structured outputs' `additionalProperties: false` rule.
"""

import copy
from typing import Any

import anthropic
from pydantic import ValidationError

from app.spec import AssistantSpec, assistant_spec_json_schema

BUILDER_MODEL = "claude-sonnet-5"
SPEC_TOOL_NAME = "save_assistant_spec"
MAX_OUTPUT_TOKENS = 4096

# The edit loop is bounded so a misbehaving model can never spin forever
# (implementation rule #4). If the cap is hit, we fall back to whole-spec
# regeneration rather than dead-ending the edit.
MAX_EDIT_ITERATIONS = 8

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


# --- Edit path: bounded tool-calling agent loop ---------------------------
#
# The model mutates an existing spec with targeted, named tools (rather than
# regenerating the whole thing). Guards are built in from line one: a hard
# iteration cap, invalid-argument feedback so the model self-corrects instead
# of the loop throwing, and a fallback to the create path so an edit can never
# dead-end (docs/DECISIONS.md "edit fallback").

EDIT_SYSTEM_PROMPT = (
    "You are the editor for a voice AI assistant platform. You are given the "
    "current Assistant Spec and a plain-language edit request. Apply the "
    "request by calling the mutation tools, one change at a time. Available "
    'tool registry keys are "reach", "qualify", and "book". When every '
    "requested change has been applied, call the finish tool. If a tool "
    "reports an error, correct the arguments and try again."
)

_STRING_FIELD_TOOLS = {
    "set_name": ("name", "the assistant's short title"),
    "set_objective": ("objective", "the one-sentence objective"),
    "set_persona": ("persona", "the voice/manner description"),
}


def _edit_tools() -> list[dict[str, Any]]:
    """Tool schemas for the edit loop. Field shapes mirror AssistantSpec — the
    single schema stays the source of truth; nothing here is hand-duplicated."""
    tools: list[dict[str, Any]] = [
        {
            "name": tool_name,
            "description": f"Replace {description}.",
            "input_schema": {
                "type": "object",
                "properties": {field: {"type": "string"}},
                "required": [field],
            },
        }
        for tool_name, (field, description) in _STRING_FIELD_TOOLS.items()
    ]
    tools.append(
        {
            "name": "set_instructions",
            "description": "Replace the full list of behavioral instructions.",
            "input_schema": {
                "type": "object",
                "properties": {"instructions": {"type": "array", "items": {"type": "string"}}},
                "required": ["instructions"],
            },
        }
    )
    tools.append(
        {
            "name": "add_tool",
            "description": "Add a tool by registry key (reach, qualify, or book).",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
    )
    tools.append(
        {
            "name": "remove_tool",
            "description": "Remove a tool by registry key.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        }
    )
    tools.append(
        {
            "name": "finish",
            "description": "Call once all requested edits have been applied.",
            "input_schema": {"type": "object", "properties": {}},
        }
    )
    return tools


def edit_spec(current: AssistantSpec, instruction: str) -> AssistantSpec:
    """Edit a spec via a bounded tool-calling loop; fall back to whole-spec
    regeneration if the loop can't finish cleanly, so it never dead-ends."""
    client = anthropic.Anthropic()
    working = current.model_dump()
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                f"Current spec:\n{current.model_dump_json(indent=2)}\n\n"
                f"Edit request: {instruction}"
            ),
        }
    ]

    for _ in range(MAX_EDIT_ITERATIONS):
        try:
            response = client.messages.create(
                model=BUILDER_MODEL,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=EDIT_SYSTEM_PROMPT,
                tools=_edit_tools(),
                messages=messages,
            )
        except anthropic.APIError:
            # LLM/network failure mid-loop: don't dead-end, regenerate instead.
            return _regenerate(current, instruction)

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [block for block in response.content if block.type == "tool_use"]

        if not tool_uses:
            # Model stopped without a tool call — treat the working copy as final.
            return _finalize(working, current, instruction)

        tool_results = []
        finished = False
        for block in tool_uses:
            if block.name == "finish":
                finished = True
                result_text = "Finalizing the spec."
            else:
                # A bad argument returns an error string (never raises), which is
                # fed back so the model self-corrects on the next turn (rule #4).
                result_text = _apply_mutation(working, block.name, block.input)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
            )

        messages.append({"role": "user", "content": tool_results})

        if finished:
            return _finalize(working, current, instruction)

    # Iteration cap hit without a finish — fall back so the edit can't dead-end.
    return _regenerate(current, instruction)


def _apply_mutation(working: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    """Apply one mutation to the working spec dict. Mutates only on success;
    returns a human-readable result line, or an error string to feed back.
    Never raises — invalid arguments become feedback, not crashes."""
    candidate = copy.deepcopy(working)
    try:
        if name in _STRING_FIELD_TOOLS:
            field, _ = _STRING_FIELD_TOOLS[name]
            candidate[field] = args[field]
        elif name == "set_instructions":
            candidate["instructions"] = args["instructions"]
        elif name == "add_tool":
            tool_name = args["name"]
            if any(tool["name"] == tool_name for tool in candidate["tools"]):
                return f"Tool '{tool_name}' is already present; no change made."
            candidate["tools"].append({"name": tool_name, "settings": {}})
        elif name == "remove_tool":
            tool_name = args["name"]
            remaining = [tool for tool in candidate["tools"] if tool["name"] != tool_name]
            if len(remaining) == len(candidate["tools"]):
                return f"No tool named '{tool_name}' to remove."
            candidate["tools"] = remaining
        else:
            return f"Unknown tool '{name}'."
    except KeyError as missing_arg:
        return f"Missing required argument for {name}: {missing_arg}."

    # Validate the whole candidate so bad values (wrong types, etc.) surface now.
    try:
        AssistantSpec.model_validate(candidate)
    except ValidationError as validation_error:
        return f"That change would make the spec invalid: {validation_error}"

    working.clear()
    working.update(candidate)
    return f"Applied {name} successfully."


def _finalize(working: dict[str, Any], current: AssistantSpec, instruction: str) -> AssistantSpec:
    """Validate the edited working copy; regenerate if it somehow isn't valid."""
    try:
        return AssistantSpec.model_validate(working)
    except ValidationError:
        return _regenerate(current, instruction)


def _regenerate(current: AssistantSpec, instruction: str) -> AssistantSpec:
    """Fallback: rebuild the whole spec via the reliable create path so an edit
    request never dead-ends. Raises BuilderError only if create itself fails."""
    return create_spec(_synthesize_description(current, instruction))


def _synthesize_description(current: AssistantSpec, instruction: str) -> str:
    tool_names = ", ".join(tool.name for tool in current.tools) or "none"
    return (
        f"An assistant named '{current.name}'. Objective: {current.objective}. "
        f"Persona: {current.persona}. Current tools: {tool_names}. "
        f"Apply this change and produce the full updated spec: {instruction}"
    )

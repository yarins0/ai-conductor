"""Edit-path tests: the Anthropic client is faked, so no network calls.

The fakes mimic just the shape edit_spec reads: a response with `.content`
holding tool_use blocks that expose `.type` / `.name` / `.input` / `.id`.
"""

import types

import anthropic
import httpx
import pytest

from app import builder, db
from app.spec import AssistantSpec, ToolConfig

SAMPLE_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Call leads, qualify budget, and book a demo.",
    persona="Friendly and direct.",
    instructions=["Always confirm the lead's name."],
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify")],
)


def _block(name: str, args: dict | None = None, block_id: str = "tool_1") -> types.SimpleNamespace:
    return types.SimpleNamespace(type="tool_use", name=name, input=args or {}, id=block_id)


def _response(*blocks: types.SimpleNamespace) -> types.SimpleNamespace:
    return types.SimpleNamespace(content=list(blocks))


def _fake_client_returning(responses: list) -> types.SimpleNamespace:
    """A fake Anthropic client that returns the scripted responses in order."""
    state = {"index": 0}

    def create(**_kwargs):
        response = responses[state["index"]]
        state["index"] += 1
        return response

    return types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


def _patch_client(monkeypatch, client: types.SimpleNamespace) -> None:
    monkeypatch.setattr(builder.anthropic, "Anthropic", lambda: client)


def test_create_spec_invalid_output_raises_builder_error(monkeypatch):
    # Missing required field `objective` — boundary validation (rule #2) must
    # reject this before anything is persisted.
    client = _fake_client_returning([
        _response(_block("save_assistant_spec", {"name": "X", "persona": "Y", "tools": []})),
    ])
    _patch_client(monkeypatch, client)

    with pytest.raises(builder.BuilderError):
        builder.create_spec("anything")


def test_create_spec_no_tool_use_raises_builder_error(monkeypatch):
    # A refusal or plain-text response has no tool_use block at all.
    client = _fake_client_returning([
        types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="refused")]),
    ])
    _patch_client(monkeypatch, client)

    with pytest.raises(builder.BuilderError):
        builder.create_spec("anything")


def test_edit_falls_back_when_llm_errors(monkeypatch):
    # A mid-loop API failure must not dead-end the edit (rule #4) — it falls
    # back to whole-spec regeneration via create_spec.
    def raising_create(**_kwargs):
        raise anthropic.APIError(
            "boom", request=httpx.Request("POST", "https://api.anthropic.com"), body=None
        )

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=raising_create))
    _patch_client(monkeypatch, client)

    sentinel = AssistantSpec(
        name="Sentinel Assistant",
        objective="Stand in for a regenerated spec.",
        persona="N/A",
        tools=[ToolConfig(name="reach")],
    )
    monkeypatch.setattr(builder, "create_spec", lambda description: sentinel)

    result = builder.edit_spec(SAMPLE_SPEC, "anything")

    assert result is sentinel


def test_edit_applies_mutations_then_finishes(monkeypatch):
    client = _fake_client_returning([
        _response(_block("set_objective", {"objective": "Qualify and book enterprise demos."})),
        _response(_block("add_tool", {"name": "book"})),
        _response(_block("finish")),
    ])
    _patch_client(monkeypatch, client)

    result = builder.edit_spec(SAMPLE_SPEC, "focus on enterprise and add booking")

    assert result.objective == "Qualify and book enterprise demos."
    assert [tool.name for tool in result.tools] == ["reach", "qualify", "book"]


def test_edit_recovers_from_invalid_tool_argument(monkeypatch):
    # First turn asks to remove a tool that isn't there — a soft error that must
    # be fed back (not raised); the model then makes a valid edit and finishes.
    client = _fake_client_returning([
        _response(_block("remove_tool", {"name": "nonexistent"})),
        _response(_block("set_persona", {"persona": "Warm and consultative."})),
        _response(_block("finish")),
    ])
    _patch_client(monkeypatch, client)

    result = builder.edit_spec(SAMPLE_SPEC, "make it warmer")

    assert result.persona == "Warm and consultative."
    # The bogus removal changed nothing; the original tools are intact.
    assert [tool.name for tool in result.tools] == ["reach", "qualify"]


def test_edit_falls_back_to_create_when_cap_is_hit(monkeypatch):
    # Model never calls finish — every turn mutates, so the iteration cap trips
    # and the edit falls back to whole-spec regeneration rather than dead-ending.
    def never_finishing_create(**_kwargs):
        return _response(_block("set_objective", {"objective": "still going"}))

    client = types.SimpleNamespace(messages=types.SimpleNamespace(create=never_finishing_create))
    _patch_client(monkeypatch, client)

    regenerated = AssistantSpec(
        name="Regenerated Assistant",
        objective="Rebuilt from scratch.",
        persona="Neutral.",
        tools=[ToolConfig(name="reach")],
    )
    monkeypatch.setattr(builder, "create_spec", lambda description: regenerated)

    result = builder.edit_spec(SAMPLE_SPEC, "loop forever")

    assert result is regenerated


def test_update_spec_persists_change_and_returns_none_when_missing():
    record = db.save_spec(SAMPLE_SPEC)
    edited = SAMPLE_SPEC.model_copy(update={"objective": "Edited objective."})

    updated = db.update_spec(record.id, edited)

    assert updated is not None
    assert "Edited objective." in updated.spec_json
    reloaded = db.get_spec(record.id)
    assert AssistantSpec.model_validate_json(reloaded.spec_json).objective == "Edited objective."

    assert db.update_spec(999999, edited) is None

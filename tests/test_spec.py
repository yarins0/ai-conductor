import pytest
from pydantic import ValidationError

from app.spec import AssistantSpec, assistant_spec_json_schema


def test_valid_spec_parses() -> None:
    spec = AssistantSpec(
        name="Demo Assistant",
        objective="Book a meeting with qualified leads",
        persona="Friendly, concise, professional",
        instructions=["Always confirm the lead's name before qualifying"],
        tools=[{"name": "reach", "settings": {}}, {"name": "book", "settings": {"duration_minutes": 30}}],
    )
    assert spec.name == "Demo Assistant"
    assert spec.tools[0].name == "reach"


def test_missing_objective_rejected() -> None:
    with pytest.raises(ValidationError):
        AssistantSpec(
            name="Demo Assistant",
            persona="Friendly",
            tools=[{"name": "reach"}],
        )


def test_tools_not_a_list_rejected() -> None:
    with pytest.raises(ValidationError):
        AssistantSpec(
            name="Demo Assistant",
            objective="Book meetings",
            persona="Friendly",
            tools={"name": "reach"},
        )


def test_json_schema_contains_required_fields() -> None:
    schema = assistant_spec_json_schema()
    assert isinstance(schema, dict)
    assert set(schema["required"]) >= {"name", "objective", "persona", "tools"}

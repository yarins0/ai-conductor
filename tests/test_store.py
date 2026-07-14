from app.db import get_spec, list_specs, save_spec
from app.spec import AssistantSpec


def _make_spec(name: str = "Demo Assistant") -> AssistantSpec:
    return AssistantSpec(
        name=name,
        objective="Book a meeting with qualified leads",
        persona="Friendly, concise, professional",
        instructions=["Confirm the lead's name first"],
        tools=[{"name": "reach", "settings": {}}],
    )


def test_save_then_get_round_trips() -> None:
    record = save_spec(_make_spec())

    fetched = get_spec(record.id)

    assert fetched is not None
    assert AssistantSpec.model_validate_json(fetched.spec_json) == _make_spec()


def test_get_missing_spec_returns_none() -> None:
    assert get_spec(999) is None


def test_list_specs_returns_newest_first() -> None:
    first = save_spec(_make_spec("First"))
    second = save_spec(_make_spec("Second"))

    records = list_specs()

    ids_in_order = [record.id for record in records]
    assert ids_in_order.index(second.id) < ids_in_order.index(first.id)

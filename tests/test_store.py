from app.db import (
    add_run_step,
    create_run,
    finish_run,
    get_lead,
    get_run,
    get_spec,
    list_leads,
    list_run_steps,
    list_runs,
    list_specs,
    save_spec,
    seed_leads,
    update_lead_outcome,
)
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


def test_seed_leads_creates_one_per_sim_profile() -> None:
    leads = list_leads()

    assert len(leads) == 3
    assert {lead.sim_profile for lead in leads} == {
        "books",
        "no_answer",
        "not_qualified",
    }


def test_seed_leads_is_idempotent() -> None:
    seed_leads()

    assert len(list_leads()) == 3


def test_get_lead_found_and_missing() -> None:
    seeded = list_leads()[0]

    assert get_lead(seeded.id) is not None
    assert get_lead(999999) is None


def test_update_lead_outcome_sets_status_and_optional_fields() -> None:
    lead = list_leads()[0]

    updated = update_lead_outcome(lead.id, "booked", intent_score=8, booked_slot="2026-07-15T10:00:00Z")

    assert updated is not None
    assert updated.status == "booked"
    assert updated.intent_score == 8
    assert updated.booked_slot == "2026-07-15T10:00:00Z"


def test_update_lead_outcome_missing_lead_returns_none() -> None:
    assert update_lead_outcome(999999, "booked") is None


def test_update_lead_outcome_does_not_clobber_intent_score_with_none() -> None:
    lead = list_leads()[0]
    update_lead_outcome(lead.id, "booked", intent_score=9)

    later = update_lead_outcome(lead.id, "unreachable")

    assert later is not None
    assert later.status == "unreachable"
    assert later.intent_score == 9


def test_run_lifecycle() -> None:
    lead = list_leads()[0]
    spec = save_spec(_make_spec("Run Lifecycle Spec"))

    run = create_run(spec.id, lead.id)
    assert run.status == "running"

    finished = finish_run(run.id, "completed")
    assert finished is not None
    assert finished.status == "completed"

    assert finish_run(999999, "completed") is None
    assert get_run(999999) is None
    assert get_run(run.id) is not None


def test_run_steps_append_in_execution_order() -> None:
    lead = list_leads()[0]
    spec = save_spec(_make_spec("Run Steps Spec"))
    run = create_run(spec.id, lead.id)

    add_run_step(run.id, "reach", '{"outcome": "answered"}')
    add_run_step(run.id, "qualify", '{"outcome": "qualified"}')
    add_run_step(run.id, "book", '{"outcome": "booked"}')

    steps = list_run_steps(run.id)

    assert [step.tool for step in steps] == ["reach", "qualify", "book"]


def test_list_runs_returns_newest_first() -> None:
    lead = list_leads()[0]
    spec = save_spec(_make_spec("List Runs Spec"))
    first = create_run(spec.id, lead.id)
    second = create_run(spec.id, lead.id)

    runs = list_runs()

    ids_in_order = [run.id for run in runs]
    assert ids_in_order.index(second.id) < ids_in_order.index(first.id)

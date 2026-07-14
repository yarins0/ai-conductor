import asyncio
import json

import pytest

import app.providers as providers
from app import db
from app.runtime import execute_run
from app.spec import AssistantSpec, ToolConfig
from app.providers import get_provider

SAMPLE_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Call leads, qualify budget, and book a demo.",
    persona="Friendly and direct.",
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)


@pytest.fixture(autouse=True)
def _no_step_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "STEP_DELAY_SECONDS", 0)


def _lead_by_profile(sim_profile: str) -> db.LeadRecord:
    return next(lead for lead in db.list_leads() if lead.sim_profile == sim_profile)


def test_happy_path_books_lead() -> None:
    lead = _lead_by_profile("books")
    spec_record = db.save_spec(SAMPLE_SPEC)
    run = db.create_run(spec_id=spec_record.id, lead_id=lead.id)

    asyncio.run(execute_run(run.id, SAMPLE_SPEC, lead))

    steps = db.list_run_steps(run.id)
    assert [step.tool for step in steps] == ["reach", "qualify", "book"]

    finished_run = db.get_run(run.id)
    assert finished_run.status == "completed"

    updated_lead = db.get_lead(lead.id)
    assert updated_lead.status == "booked"
    assert updated_lead.intent_score is not None
    assert updated_lead.booked_slot is not None


def test_no_answer_lead_stops_after_reach() -> None:
    lead = _lead_by_profile("no_answer")
    spec_record = db.save_spec(SAMPLE_SPEC)
    run = db.create_run(spec_id=spec_record.id, lead_id=lead.id)

    asyncio.run(execute_run(run.id, SAMPLE_SPEC, lead))

    steps = db.list_run_steps(run.id)
    assert [step.tool for step in steps] == ["reach"]

    finished_run = db.get_run(run.id)
    assert finished_run.status == "completed"

    updated_lead = db.get_lead(lead.id)
    assert updated_lead.status == "unreachable"


def test_not_qualified_lead_stops_after_qualify() -> None:
    lead = _lead_by_profile("not_qualified")
    spec_record = db.save_spec(SAMPLE_SPEC)
    run = db.create_run(spec_id=spec_record.id, lead_id=lead.id)

    asyncio.run(execute_run(run.id, SAMPLE_SPEC, lead))

    steps = db.list_run_steps(run.id)
    assert [step.tool for step in steps] == ["reach", "qualify"]

    finished_run = db.get_run(run.id)
    assert finished_run.status == "completed"

    updated_lead = db.get_lead(lead.id)
    assert updated_lead.status == "not_qualified"
    assert updated_lead.intent_score is not None


def test_provider_exception_fails_run_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    # A provider bug must never crash the server or strand a run (runtime.py
    # comment) — it should persist an error step and fail the run cleanly.
    lead = _lead_by_profile("books")
    spec = AssistantSpec(
        name="Flaky Assistant",
        objective="Trip over a broken provider.",
        persona="N/A",
        tools=[ToolConfig(name="reach"), ToolConfig(name="qualify")],
    )
    spec_record = db.save_spec(spec)
    run = db.create_run(spec_id=spec_record.id, lead_id=lead.id)

    async def _raise(lead, settings):
        raise RuntimeError("boom")

    monkeypatch.setattr(get_provider("reach"), "execute", _raise)

    asyncio.run(execute_run(run.id, spec, lead))

    steps = db.list_run_steps(run.id)
    assert len(steps) == 1
    error_result = json.loads(steps[0].result_json)
    assert error_result["status"] == "error"
    assert error_result["outcome"] == "provider_error"

    finished_run = db.get_run(run.id)
    assert finished_run.status == "failed"


def test_unregistered_tool_persists_error_and_fails_run() -> None:
    lead = _lead_by_profile("books")
    bad_spec = AssistantSpec(
        name="Broken Assistant",
        objective="Break on an unknown tool.",
        persona="N/A",
        tools=[ToolConfig(name="reach"), ToolConfig(name="teleport")],
    )
    spec_record = db.save_spec(bad_spec)
    run = db.create_run(spec_id=spec_record.id, lead_id=lead.id)

    asyncio.run(execute_run(run.id, bad_spec, lead))

    steps = db.list_run_steps(run.id)
    assert [step.tool for step in steps] == ["reach", "teleport"]
    error_result = json.loads(steps[-1].result_json)
    assert error_result["status"] == "error"
    assert error_result["outcome"] == "unknown_tool"

    finished_run = db.get_run(run.id)
    assert finished_run.status == "failed"

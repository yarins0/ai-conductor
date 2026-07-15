"""Unit tests for the stateless tool executor (app/tool_exec.py)."""

import asyncio

import pytest

from app import db, tool_exec
from app.providers import ProviderConfigError, ToolResult
from app.providers.base import Provider
from app.spec import AssistantSpec, ToolConfig

SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Call leads, qualify budget, and book a demo.",
    persona="Friendly and direct.",
    instructions=[],
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)


class StubProvider(Provider):
    """Scriptable provider: returns a fixed result, raises, or fails preflight —
    and records the settings dict it was called with for merge assertions."""

    def __init__(self, result=None, execute_error=None, config_error=None):
        self.result = result
        self.execute_error = execute_error
        self.config_error = config_error
        self.seen_settings = None

    def check_credentials(self) -> None:
        if self.config_error:
            raise ProviderConfigError(self.config_error)

    async def execute(self, lead, settings) -> ToolResult:
        self.seen_settings = settings
        if self.execute_error:
            raise self.execute_error
        return self.result


@pytest.fixture()
def spec_id():
    db.init_db()
    return db.save_spec(SPEC).id


@pytest.fixture()
def lead():
    return db.create_lead("Test Lead", "TestCo", "+15550000000")


def _stub(monkeypatch, provider: StubProvider) -> None:
    monkeypatch.setattr(tool_exec, "get_provider", lambda tool, pid=None: provider)


def _run(coro):
    return asyncio.run(coro)


def test_tool_outside_spec_raises(spec_id, lead):
    with pytest.raises(tool_exec.ToolNotAllowed):
        _run(tool_exec.run_tool(spec_id, SPEC, lead, "send_invoice", None, {}))


def test_result_persisted_and_written_back(monkeypatch, spec_id, lead):
    _stub(
        monkeypatch,
        StubProvider(
            result=ToolResult(
                tool="book", status="ok", outcome="booked",
                summary="Booked.", data={"slot": "2026-07-16T10:00:00"},
            )
        ),
    )

    run_id, result = _run(tool_exec.run_tool(spec_id, SPEC, lead, "book", None, {}))

    assert result.outcome == "booked"
    assert [step.tool for step in db.list_run_steps(run_id)] == ["book"]
    assert db.get_lead(lead.id).status == "booked"
    assert db.get_lead(lead.id).booked_slot == "2026-07-16T10:00:00"


def test_initiated_reach_is_not_persisted(monkeypatch, spec_id, lead):
    # A Twilio reach that only *placed* the call must not write a step — the
    # media-stream bridge writes the single durable reach step when the call
    # resolves, so the placeholder would otherwise shadow the real transcript.
    _stub(
        monkeypatch,
        StubProvider(
            result=ToolResult(
                tool="reach", status="ok", outcome="initiated",
                summary="Call placed.", data={"call_sid": "CA123"},
            )
        ),
    )

    run_id, result = _run(tool_exec.run_tool(spec_id, SPEC, lead, "reach", None, {}))

    assert result.outcome == "initiated"
    assert db.list_run_steps(run_id) == []
    assert db.get_lead(lead.id).status == "new"


def test_provider_exception_becomes_error_result(monkeypatch, spec_id, lead):
    _stub(monkeypatch, StubProvider(execute_error=RuntimeError("boom")))

    run_id, result = _run(tool_exec.run_tool(spec_id, SPEC, lead, "qualify", None, {}))

    assert result.status == "error"
    assert result.outcome == "provider_error"
    # Error outcomes are still persisted — a failure is a step, not a secret.
    assert [step.tool for step in db.list_run_steps(run_id)] == ["qualify"]


def test_missing_credentials_become_config_error(monkeypatch, spec_id, lead):
    _stub(monkeypatch, StubProvider(config_error="Twilio Voice needs TWILIO_ACCOUNT_SID"))

    _, result = _run(tool_exec.run_tool(spec_id, SPEC, lead, "reach", None, {}))

    assert result.status == "error"
    assert result.outcome == "config_error"
    assert "TWILIO_ACCOUNT_SID" in result.summary


def test_transcript_threads_from_previous_step(monkeypatch, spec_id, lead):
    transcript = [{"speaker": "lead", "text": "We need this yesterday."}]
    reach_result = ToolResult(
        tool="reach", status="ok", outcome="answered",
        summary="Answered.", data={"transcript": transcript},
    )
    run = db.get_or_create_run(spec_id, lead.id)
    db.add_run_step(run.id, "reach", reach_result.model_dump_json())

    stub = StubProvider(
        result=ToolResult(tool="qualify", status="ok", outcome="qualified",
                          summary="Qualified.", data={"intent_score": 80})
    )
    _stub(monkeypatch, stub)

    run_id, _ = _run(tool_exec.run_tool(spec_id, SPEC, lead, "qualify", None, {}))

    assert run_id == run.id  # same running pair -> same run row
    assert stub.seen_settings["transcript"] == transcript


def test_model_args_cannot_overwrite_run_context(monkeypatch, spec_id, lead):
    stub = StubProvider(
        result=ToolResult(tool="book", status="ok", outcome="booked", summary="Booked.")
    )
    _stub(monkeypatch, stub)

    run_id, _ = _run(
        tool_exec.run_tool(
            spec_id, SPEC, lead, "book", None, {"run_id": 424242, "spec": "forged"}
        )
    )

    assert stub.seen_settings["run_id"] == run_id
    assert stub.seen_settings["spec"] is SPEC  # the real spec object, not the forged arg


def test_write_back_maps_outcomes():
    lead = db.create_lead("WB Lead", "WBCo", "+15550000001")
    tool_exec.write_back_lead_outcome(
        lead.id,
        ToolResult(tool="qualify", status="ok", outcome="not_qualified",
                   summary="No fit.", data={"intent_score": 5}),
    )
    refreshed = db.get_lead(lead.id)
    assert refreshed.status == "not_qualified"
    assert refreshed.intent_score == 5

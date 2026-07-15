"""Live-agent loop tests.

These exercise the loop's own logic — tool invocation, Company-Brain write-back,
persistence, and provider-failure resilience — with a faked Anthropic client, so
they are deterministic and cost nothing. Whether the *real* model picks the right
tool from open-ended speech is a model-quality question, verified in the live
manual demo, not here.
"""

import asyncio
import json

import pytest
from conftest import FakeClient, Response as _Response, text_block as _text, tool_block as _tool

import app.providers as providers
from app import db, live_agent
from app.providers import get_provider
from app.spec import AssistantSpec, ToolConfig

SAMPLE_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Have a live conversation, qualify the lead, and book a meeting.",
    persona="Friendly and direct.",
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, responses: list[_Response]) -> None:
    """Make LiveSession construct a fake client that replays `responses` in order."""
    monkeypatch.setattr(
        live_agent.anthropic, "AsyncAnthropic", lambda *a, **k: FakeClient(responses)
    )


@pytest.fixture(autouse=True)
def _no_step_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "STEP_DELAY_SECONDS", 0)


def _drive(session: live_agent.LiveSession, text: str) -> list[dict]:
    """Collect all events a single user turn yields."""

    async def _run() -> list[dict]:
        return [event async for event in session.handle_turn(text)]

    return asyncio.run(_run())


def _lead_by_profile(sim_profile: str) -> db.LeadRecord:
    # Reuse the seeded leads (never create new rows) so the store tests' exact
    # lead-count assertions stay valid — same pattern as test_runtime.py.
    return next(lead for lead in db.list_leads() if lead.sim_profile == sim_profile)


def _new_session(spec: AssistantSpec, lead: db.LeadRecord, responses: list[_Response], monkeypatch):
    _install_fake_client(monkeypatch, responses)
    spec_record = db.save_spec(spec)
    run = db.create_run(spec_id=spec_record.id, lead_id=lead.id)
    return live_agent.LiveSession(spec, lead, run.id), run


def test_invokes_book_and_writes_outcome_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # Model chooses `book`, then confirms with no further tool call. Simulated book
    # ignores sim_profile and always books, so any seeded lead exercises the path.
    lead = _lead_by_profile("books")
    session, run = _new_session(
        SAMPLE_SPEC,
        lead,
        [
            _Response([_text("Sure, booking that now."), _tool("book", "t1")]),
            _Response([_text("You're all set for tomorrow.")]),
        ],
        monkeypatch,
    )

    events = _drive(session, "can you book me a meeting")

    actions = [event for event in events if event["type"] == "action"]
    assert [action["tool"] for action in actions] == ["book"]
    assert actions[0]["result"]["outcome"] == "booked"
    # Write-back and persistence mirror a scripted run.
    assert db.get_lead(lead.id).status == "booked"
    assert [step.tool for step in db.list_run_steps(run.id)] == ["book"]
    # The pre-tool and post-tool text are both spoken.
    assert sum(1 for event in events if event["type"] == "assistant") == 2


def test_no_tool_when_model_just_talks(monkeypatch: pytest.MonkeyPatch) -> None:
    # A turn where the model declines to act must not force any tool or write-back.
    lead = _lead_by_profile("no_answer")
    status_before = db.get_lead(lead.id).status
    session, run = _new_session(
        SAMPLE_SPEC, lead, [_Response([_text("No problem — have a good day.")])], monkeypatch
    )

    events = _drive(session, "I'm not interested, thanks")

    assert not [event for event in events if event["type"] == "action"]
    assert db.get_lead(lead.id).status == status_before  # untouched by a no-tool turn
    assert db.list_run_steps(run.id) == []
    assert any(event["type"] == "assistant" for event in events)


def test_provider_failure_becomes_error_action_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # A provider bug must surface as an error outcome fed back to the model, never
    # raise into the socket — the Runtime's guarantee, kept in live mode.
    lead = _lead_by_profile("not_qualified")
    status_before = db.get_lead(lead.id).status
    session, run = _new_session(
        SAMPLE_SPEC,
        lead,
        [
            _Response([_tool("book", "t1")]),
            _Response([_text("Sorry, I hit a snag scheduling that.")]),
        ],
        monkeypatch,
    )

    async def _raise(_lead, _settings):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(get_provider("book"), "execute", _raise)

    events = _drive(session, "book me in")

    actions = [event for event in events if event["type"] == "action"]
    assert actions[0]["result"]["status"] == "error"
    assert actions[0]["result"]["outcome"] == "provider_error"
    assert db.get_lead(lead.id).status == status_before  # a failed book leaves the lead unchanged
    steps = db.list_run_steps(run.id)
    assert len(steps) == 1
    assert json.loads(steps[0].result_json)["outcome"] == "provider_error"

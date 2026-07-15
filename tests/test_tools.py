import asyncio
from datetime import datetime, timezone

import pytest
from conftest import Response, text_block, tool_block

import app.providers as providers
from app.db import LeadRecord
from app.providers import get_provider
from app.spec import AssistantSpec, ToolConfig

SAMPLE_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Qualify the lead and book a meeting.",
    persona="Friendly and direct.",
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)


@pytest.fixture(autouse=True)
def _no_step_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "STEP_DELAY_SECONDS", 0)


def _make_lead(sim_profile: str) -> LeadRecord:
    now = datetime.now(timezone.utc)
    return LeadRecord(
        name="Jane Prospect",
        company="Acme Co",
        phone="555-0100",
        sim_profile=sim_profile,
        created_at=now,
        updated_at=now,
    )


def _call_turns(count: int) -> list[Response]:
    """Enough canned turns to satisfy a full simulated call (both sides, per exchange)."""
    return [Response([text_block("Sure.")]) for _ in range(count)]


def test_registry_has_reach_qualify_book_registered() -> None:
    assert get_provider("reach") is not None
    assert get_provider("qualify") is not None
    assert get_provider("book") is not None


def test_get_provider_returns_none_for_unknown_tool() -> None:
    assert get_provider("bogus") is None


def test_reach_provider_answers_and_returns_a_transcript(fake_anthropic) -> None:
    # A reached lead now yields the actual call, not a claim that one happened.
    fake_anthropic(_call_turns(8))

    result = asyncio.run(
        get_provider("reach").execute(_make_lead("books"), {"spec": SAMPLE_SPEC})
    )

    assert result.status == "ok"
    assert result.outcome == "answered"
    assert {entry["speaker"] for entry in result.data["transcript"]} == {"assistant", "lead"}
    assert "Sure." in result.summary  # the call rides back in the summary for the agent


def test_reach_provider_no_answer_for_no_answer_profile() -> None:
    # Nobody picks up, so no conversation is attempted — and no LLM is called,
    # which is why this needs no fake client at all.
    result = asyncio.run(get_provider("reach").execute(_make_lead("no_answer"), {}))

    assert result.status == "ok"
    assert result.outcome == "no_answer"
    assert "transcript" not in result.data


def test_reach_provider_errors_without_a_spec_rather_than_faking_a_call() -> None:
    result = asyncio.run(get_provider("reach").execute(_make_lead("books"), {}))

    assert result.status == "error"
    assert result.outcome == "provider_error"


def test_qualify_scores_from_the_transcript_when_there_is_one(fake_anthropic) -> None:
    fake_anthropic(
        [Response([tool_block("record_intent_score", "s1", {"intent_score": 90, "reason": "Asked for pricing."})])]
    )
    transcript = [{"speaker": "lead", "text": "Send me pricing, we have budget."}]

    result = asyncio.run(
        get_provider("qualify").execute(_make_lead("books"), {"transcript": transcript})
    )

    assert result.outcome == "qualified"
    assert result.data["intent_score"] == 90
    assert result.data["scored_from"] == "transcript"


def test_qualify_clamps_an_out_of_range_score(fake_anthropic) -> None:
    # The model's number is untrusted input; a 900 must not reach the Brain.
    fake_anthropic(
        [Response([tool_block("record_intent_score", "s1", {"intent_score": 900, "reason": "Very keen."})])]
    )

    result = asyncio.run(
        get_provider("qualify").execute(
            _make_lead("books"), {"transcript": [{"speaker": "lead", "text": "Yes!"}]}
        )
    )

    assert result.data["intent_score"] == 100


def test_qualify_malformed_score_becomes_an_error_not_a_crash(fake_anthropic) -> None:
    fake_anthropic(
        [Response([tool_block("record_intent_score", "s1", {"intent_score": "very high"})])]
    )

    result = asyncio.run(
        get_provider("qualify").execute(
            _make_lead("books"), {"transcript": [{"speaker": "lead", "text": "Yes!"}]}
        )
    )

    assert result.status == "error"
    assert result.outcome == "provider_error"


def test_qualify_provider_qualifies_by_default() -> None:
    # No transcript (qualify called standalone) — falls back to the profile score.
    result = asyncio.run(get_provider("qualify").execute(_make_lead("books"), {}))

    assert result.outcome == "qualified"
    assert result.data["intent_score"] > 50
    assert result.data["scored_from"] == "profile"


def test_qualify_provider_rejects_not_qualified_profile() -> None:
    result = asyncio.run(get_provider("qualify").execute(_make_lead("not_qualified"), {}))

    assert result.outcome == "not_qualified"
    assert result.data["intent_score"] < 50


def test_book_provider_always_books() -> None:
    result = asyncio.run(get_provider("book").execute(_make_lead("books"), {}))

    assert result.outcome == "booked"
    assert "slot" in result.data

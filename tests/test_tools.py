import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from conftest import Response, text_block, tool_block

import app.providers as providers
from app.db import LeadRecord
from app.providers import get_provider
from app.providers.book import resolve_slot
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


# --- book slot resolution ------------------------------------------------------
#
# The regression: preferred_time was free text and every provider ignored it,
# booking now + 1 day. The assistant could agree to "tomorrow at 9" out loud
# while the calendar got 6pm — and the summary reported the wrong time honestly,
# so only the calendar itself gave it away.


def test_resolve_slot_honours_an_agreed_time() -> None:
    slot = resolve_slot({"preferred_time": "2026-07-16T09:00:00+03:00"})

    assert slot.isoformat() == "2026-07-16T09:00:00+03:00"


def test_resolve_slot_treats_a_naive_time_as_local() -> None:
    """The model dropping the offset must not silently become UTC — 9am to an
    operator means 9am where they are."""
    slot = resolve_slot({"preferred_time": "2026-07-16T09:00:00"})

    assert slot.tzinfo is not None
    assert (slot.hour, slot.minute) == (9, 0)
    assert slot.utcoffset() == datetime.now().astimezone().utcoffset()


@pytest.mark.parametrize("raw", [None, "", "tomorrow at nine", "not-a-timestamp"])
def test_resolve_slot_falls_back_a_day_ahead_rather_than_raising(raw) -> None:
    """A time nobody agreed on, or one the model mangled, still books: the
    meeting is real, so a bad timestamp costs the right hour, not the booking."""
    slot = resolve_slot({"preferred_time": raw} if raw is not None else {})

    assert abs((slot - (datetime.now(timezone.utc) + timedelta(days=1))).total_seconds()) < 5


def test_simulated_book_uses_the_agreed_time_too() -> None:
    """Both providers share resolve_slot so the simulated and real paths cannot
    disagree about when a meeting is (the sim used to hardcode one date)."""
    result = asyncio.run(
        get_provider("book").execute(
            _make_lead("books"), {"preferred_time": "2026-07-16T09:00:00+03:00"}
        )
    )

    assert result.outcome == "booked"
    assert result.data["slot"] == "2026-07-16T09:00:00+03:00"
    assert "2026-07-16T09:00:00+03:00" in result.summary


def test_simulated_book_holds_plain_time_without_a_lead() -> None:
    """Booking is not always lead work: the operator asking to block an hour has
    no lead to name the event after, so the model's title carries it."""
    result = asyncio.run(
        get_provider("book").execute(
            None, {"preferred_time": "2026-07-16T09:00:00+03:00", "title": "Team sync"}
        )
    )

    assert result.outcome == "booked"
    assert result.data["slot"] == "2026-07-16T09:00:00+03:00"
    assert "Team sync" in result.summary


def test_simulated_book_without_a_lead_or_a_title_still_names_the_event() -> None:
    result = asyncio.run(get_provider("book").execute(None, {}))

    assert result.outcome == "booked"
    assert "Meeting" in result.summary


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
    assert result.data["invited"] is False  # no email on this lead


def test_simulated_book_reports_invited_when_lead_has_an_email() -> None:
    lead = _make_lead("books").model_copy(update={"email": "dana@acme.com"})

    result = asyncio.run(get_provider("book").execute(lead, {}))

    assert result.data["invited"] is True

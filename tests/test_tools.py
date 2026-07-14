import asyncio
from datetime import datetime, timezone

import pytest

import app.providers as providers
from app.db import LeadRecord
from app.providers import get_provider


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


def test_registry_has_reach_qualify_book_registered() -> None:
    assert get_provider("reach") is not None
    assert get_provider("qualify") is not None
    assert get_provider("book") is not None


def test_get_provider_returns_none_for_unknown_tool() -> None:
    assert get_provider("bogus") is None


def test_reach_provider_answers_by_default() -> None:
    result = asyncio.run(get_provider("reach").execute(_make_lead("books"), {}))

    assert result.status == "ok"
    assert result.outcome == "answered"


def test_reach_provider_no_answer_for_no_answer_profile() -> None:
    result = asyncio.run(get_provider("reach").execute(_make_lead("no_answer"), {}))

    assert result.status == "ok"
    assert result.outcome == "no_answer"


def test_qualify_provider_qualifies_by_default() -> None:
    result = asyncio.run(get_provider("qualify").execute(_make_lead("books"), {}))

    assert result.outcome == "qualified"
    assert result.data["intent_score"] > 50


def test_qualify_provider_rejects_not_qualified_profile() -> None:
    result = asyncio.run(get_provider("qualify").execute(_make_lead("not_qualified"), {}))

    assert result.outcome == "not_qualified"
    assert result.data["intent_score"] < 50


def test_book_provider_always_books() -> None:
    result = asyncio.run(get_provider("book").execute(_make_lead("books"), {}))

    assert result.outcome == "booked"
    assert "slot" in result.data

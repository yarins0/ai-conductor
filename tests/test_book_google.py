"""GoogleCalendarBookProvider — the invite is attempted, not assumed.

No network: `service_account.Credentials.from_service_account_file` is faked to
avoid needing a real key file, and `httpx.AsyncClient.post` is faked keyed on
whether the outgoing body carries `attendees`, so these assert the exact
attempt-then-fallback shape the provider is built around.
"""

import asyncio
from typing import Any

import httpx
import pytest
from google.oauth2 import service_account

from app import db
from app.providers.book import GoogleCalendarBookProvider

CREDS_PATH = "fake-service-account.json"


class _FakeCredentials:
    token = "fake-token"

    def refresh(self, request: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _fake_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CREDENTIALS", CREDS_PATH)
    monkeypatch.setattr(
        service_account.Credentials,
        "from_service_account_file",
        lambda *a, **k: _FakeCredentials(),
    )


def _lead(email: str | None = "dana@acme.com") -> db.LeadRecord:
    return db.create_lead("Dana", "Acme Co", "555-0100", email=email)


def _install_calendar_fake(monkeypatch, *, reject_attendees: bool, reason: str = "forbiddenForServiceAccounts"):
    """Fakes the Calendar API events.insert call. If `reject_attendees`, a request
    carrying `attendees` gets the given error `reason`; any request without one
    (the fallback, or a no-email booking) always succeeds."""
    calls: list[dict[str, Any]] = []

    async def _post(self, url, **kwargs):
        body = kwargs.get("json", {})
        calls.append({"json": body, "params": kwargs.get("params", {})})
        request = httpx.Request("POST", url)
        if reject_attendees and "attendees" in body:
            return httpx.Response(
                403,
                json={"error": {"errors": [{"reason": reason}]}},
                request=request,
            )
        return httpx.Response(
            200, json={"id": "evt_1", "htmlLink": "https://calendar.example/evt_1"}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return calls


def test_invite_succeeds_when_attendee_is_accepted(monkeypatch):
    calls = _install_calendar_fake(monkeypatch, reject_attendees=False)
    lead = _lead()

    result = asyncio.run(GoogleCalendarBookProvider().execute(lead, {}))

    assert len(calls) == 1
    assert calls[0]["json"]["attendees"] == [{"email": "dana@acme.com"}]
    assert calls[0]["params"] == {"sendUpdates": "all"}
    assert result.data["invited"] is True
    assert "invited" in result.summary.lower()

    db.delete_lead(lead.id)


def test_forbidden_for_service_account_falls_back_to_booking_without_invite(monkeypatch):
    calls = _install_calendar_fake(monkeypatch, reject_attendees=True)
    lead = _lead()

    result = asyncio.run(GoogleCalendarBookProvider().execute(lead, {}))

    assert len(calls) == 2
    assert "attendees" in calls[0]["json"]
    assert "attendees" not in calls[1]["json"]
    assert result.data["invited"] is False
    assert "was not invited" in result.summary

    db.delete_lead(lead.id)


def test_no_email_never_attempts_an_invite(monkeypatch):
    calls = _install_calendar_fake(monkeypatch, reject_attendees=True)  # would reject if ever tried
    lead = _lead(email=None)

    result = asyncio.run(GoogleCalendarBookProvider().execute(lead, {}))

    assert len(calls) == 1
    assert "attendees" not in calls[0]["json"]
    assert result.data["invited"] is False
    assert "invited" not in result.summary.lower()

    db.delete_lead(lead.id)


def test_a_different_403_reason_still_propagates(monkeypatch):
    _install_calendar_fake(monkeypatch, reject_attendees=True, reason="calendarUsageLimitExceeded")
    lead = _lead()

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(GoogleCalendarBookProvider().execute(lead, {}))

    db.delete_lead(lead.id)

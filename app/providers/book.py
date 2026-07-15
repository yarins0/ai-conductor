"""Book tool providers: a simulated calendar booking (default) and a real calendar adapter."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider

GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GOOGLE_CALENDAR_MEETING_MINUTES = 30
DEFAULT_SLOT_DAYS_AHEAD = 1


def resolve_slot(settings: dict[str, Any]) -> datetime:
    """The meeting time the conversation actually agreed on.

    `preferred_time` arrives as an ISO 8601 string the model resolved against the
    current time in its instructions — it used to be free text, and every
    provider ignored it and booked now + 1 day, so an assistant could agree to
    "tomorrow at 9" out loud and put 6pm in the calendar. Shared by both
    providers so the simulated and real paths cannot disagree about when a
    meeting is.

    Falls back to +1 day for a book with no time agreed. Anything unparseable
    takes the same fallback rather than raising: the meeting is real and already
    agreed, so a bad timestamp should cost the right hour, not the booking.
    """
    raw = settings.get("preferred_time")
    if raw:
        try:
            slot = datetime.fromisoformat(str(raw))
            # A naive timestamp means the model omitted the offset; the operator
            # meant their own wall clock, which is this machine's.
            return slot if slot.tzinfo else slot.astimezone()
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc) + timedelta(days=DEFAULT_SLOT_DAYS_AHEAD)


class SimulatedBookProvider(Provider):
    # Book is only reached on the qualified path (Runtime branching), so it always
    # books; it ignores sim_profile entirely.
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(providers.STEP_DELAY_SECONDS)

        slot = resolve_slot(settings).isoformat()
        return ToolResult(
            tool="book",
            status="ok",
            outcome="booked",
            summary=f"Booked a meeting for {lead.name} at {slot}.",
            data={"slot": slot},
        )


class GoogleCalendarBookProvider(CredentialGatedProvider):
    label = "Google Calendar"
    required_env = ["GOOGLE_CALENDAR_CREDENTIALS"]

    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        creds_path = os.environ["GOOGLE_CALENDAR_CREDENTIALS"]
        # ponytail: "primary" only works if GOOGLE_CALENDAR_CREDENTIALS is a user
        # OAuth token. A service-account key (the common case) has no usable
        # primary calendar — share a real calendar with the service account's
        # email and put its ID here instead.
        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

        credentials = service_account.Credentials.from_service_account_file(
            creds_path, scopes=GOOGLE_CALENDAR_SCOPES
        )
        # Sync call (mints a short-lived OAuth token) inside an async method — fine for
        # one request; wrap in asyncio.to_thread if this path needs real concurrency.
        credentials.refresh(GoogleAuthRequest())

        slot_start = resolve_slot(settings)
        slot_end = slot_start + timedelta(minutes=GOOGLE_CALENDAR_MEETING_MINUTES)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                headers={"Authorization": f"Bearer {credentials.token}"},
                json={
                    "summary": f"Intro call with {lead.name}",
                    "description": f"Booked by ai-conductor for {lead.company}.",
                    "start": {"dateTime": slot_start.isoformat()},
                    "end": {"dateTime": slot_end.isoformat()},
                },
            )
        response.raise_for_status()
        event = response.json()

        return ToolResult(
            tool="book",
            status="ok",
            outcome="booked",
            summary=f"Booked a meeting for {lead.name} at {slot_start.isoformat()} (Google Calendar).",
            data={"slot": slot_start.isoformat(), "event_id": event["id"], "event_link": event.get("htmlLink")},
        )


register_provider("book", "sim", SimulatedBookProvider(), label="Simulated", default=True)
register_provider("book", "google", GoogleCalendarBookProvider(), label=GoogleCalendarBookProvider.label)

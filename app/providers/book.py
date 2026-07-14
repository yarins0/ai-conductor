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


class SimulatedBookProvider(Provider):
    # Book is only reached on the qualified path (Runtime branching), so it always
    # books; it ignores sim_profile entirely.
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(providers.STEP_DELAY_SECONDS)

        slot = "2026-07-15T10:00:00"
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

        slot_start = datetime.now(timezone.utc) + timedelta(days=1)
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

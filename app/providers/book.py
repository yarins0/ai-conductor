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
DEFAULT_EVENT_TITLE = "Meeting"

# Booking a meeting the lead is never told about is a real outcome, but only if
# it says so; claiming a meeting was set up with someone who never heard about
# it is the same lie as an intent score no call earned. Only surfaced when there
# is an email — with none on file there was nobody to invite in the first place.
NO_INVITE_NOTE = (
    "The lead was not invited: this calendar is reached with a service account, "
    "which Google does not allow to invite attendees."
)


async def _post_event(
    client: httpx.AsyncClient,
    calendar_id: str,
    token: str,
    body: dict[str, Any],
    *,
    send_updates: str | None = None,
) -> httpx.Response:
    return await client.post(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
        headers={"Authorization": f"Bearer {token}"},
        params={"sendUpdates": send_updates} if send_updates else {},
        json=body,
    )


def _is_forbidden_for_service_account(response: httpx.Response) -> bool:
    # A service account without Domain-Wide Delegation of Authority rejects the
    # *whole* event-creation request when it carries attendees — verified
    # against live credentials (see docs/DECISIONS.md). Checked by reason, not
    # just status_code == 403: a different 403 (bad scope, calendar not shared)
    # is a real failure and must still propagate, not be swallowed as this one.
    if response.status_code != 403:
        return False
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except ValueError:
        return False
    return any(error.get("reason") == "forbiddenForServiceAccounts" for error in errors)


def event_title(lead: LeadRecord | None, settings: dict[str, Any]) -> str:
    """What to call the event: the model's title if it gave one, else the lead it
    is with, else a bare label for plain time held on the operator's calendar."""
    title = settings.get("title")
    if title:
        return str(title)
    return f"Intro call with {lead.name}" if lead else DEFAULT_EVENT_TITLE


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
    requires_lead = False  # holding plain time needs no lead; see Provider.requires_lead

    async def execute(self, lead: LeadRecord | None, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(providers.STEP_DELAY_SECONDS)

        slot = resolve_slot(settings).isoformat()
        title = event_title(lead, settings)
        return ToolResult(
            tool="book",
            status="ok",
            outcome="booked",
            summary=(
                f"Booked a meeting for {lead.name} at {slot}."
                if lead
                else f"Booked '{title}' at {slot}."
            ),
            # No real API to fail against here, so parity with the real
            # provider's now-genuine `invited` field is unconditional.
            data={"slot": slot, "invited": bool(lead and lead.email)},
        )


class GoogleCalendarBookProvider(CredentialGatedProvider):
    label = "Google Calendar"
    required_env = ["GOOGLE_CALENDAR_CREDENTIALS"]
    requires_lead = False  # see SimulatedBookProvider

    async def execute(self, lead: LeadRecord | None, settings: dict[str, Any]) -> ToolResult:
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
        title = event_title(lead, settings)
        event_body: dict[str, Any] = {
            "summary": title,
            "description": (
                f"Booked by ai-conductor for {lead.company}."
                if lead
                else "Booked by ai-conductor."
            ),
            "start": {"dateTime": slot_start.isoformat()},
            "end": {"dateTime": slot_end.isoformat()},
        }

        invited = False
        async with httpx.AsyncClient() as client:
            if lead and lead.email:
                # Attempted, not assumed: only a live 403 with this exact reason
                # falls back to booking without the invite (see docs/DECISIONS.md).
                response = await _post_event(
                    client,
                    calendar_id,
                    credentials.token,
                    {**event_body, "attendees": [{"email": lead.email}]},
                    send_updates="all",
                )
                if _is_forbidden_for_service_account(response):
                    response = await _post_event(client, calendar_id, credentials.token, event_body)
                else:
                    invited = True
            else:
                response = await _post_event(client, calendar_id, credentials.token, event_body)
        response.raise_for_status()
        event = response.json()

        slot = slot_start.isoformat()
        summary = (
            f"Booked a meeting for {lead.name} at {slot} (Google Calendar)."
            if lead
            else f"Booked '{title}' at {slot} (Google Calendar)."
        )
        if lead and lead.email:
            summary = f"{summary} {'The lead was invited.' if invited else NO_INVITE_NOTE}"

        return ToolResult(
            tool="book",
            status="ok",
            outcome="booked",
            summary=summary,
            data={
                "slot": slot,
                "event_id": event["id"],
                "event_link": event.get("htmlLink"),
                "invited": invited,
            },
        )


register_provider("book", "sim", SimulatedBookProvider(), label="Simulated", default=True)
register_provider("book", "google", GoogleCalendarBookProvider(), label=GoogleCalendarBookProvider.label)

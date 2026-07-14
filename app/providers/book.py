"""Book tool providers: a simulated calendar booking (default) and a real calendar adapter."""

import asyncio
from typing import Any

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider


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


register_provider("book", "sim", SimulatedBookProvider(), label="Simulated", default=True)
register_provider("book", "google", GoogleCalendarBookProvider(), label=GoogleCalendarBookProvider.label)

"""Simulated Providers for the reach / qualify / book tools.

Each Provider is a swappable adapter behind the fixed Provider interface —
simulated today, a real integration later, with no change to the Runtime.
Outcomes are derived deterministically from lead.sim_profile rather than
randomized, so the demo reliably exercises every branch on every run.
"""

import asyncio
from typing import Any

from app.db import LeadRecord
from app.tools import Provider, ToolResult, register_provider

STEP_DELAY_SECONDS = 0.5  # tests monkeypatch this to 0; read via module attribute below


class SimulatedReachProvider(Provider):
    # Real drop-in: a telephony/voice adapter (e.g. Twilio Voice / Alta's calling stack).
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(STEP_DELAY_SECONDS)

        if lead.sim_profile == "no_answer":
            return ToolResult(
                tool="reach",
                status="ok",
                outcome="no_answer",
                summary=f"Called {lead.phone} — no answer.",
                data={"channel": "voice"},
            )

        return ToolResult(
            tool="reach",
            status="ok",
            outcome="answered",
            summary=f"Called {lead.name} at {lead.phone} — they picked up.",
            data={"channel": "voice"},
        )


class SimulatedQualifyProvider(Provider):
    # Real drop-in: a CRM/intent-scoring adapter (e.g. HubSpot/Salesforce enrichment
    # or an LLM scoring call).
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(STEP_DELAY_SECONDS)

        if lead.sim_profile == "not_qualified":
            intent_score = 25
            return ToolResult(
                tool="qualify",
                status="ok",
                outcome="not_qualified",
                summary=f"Qualification score {intent_score} — below threshold.",
                data={"intent_score": intent_score},
            )

        intent_score = 85
        return ToolResult(
            tool="qualify",
            status="ok",
            outcome="qualified",
            summary=f"Qualification score {intent_score} — qualified.",
            data={"intent_score": intent_score},
        )


class SimulatedBookProvider(Provider):
    # Real drop-in: a calendar adapter (e.g. Google Calendar / Calendly API).
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(STEP_DELAY_SECONDS)

        # Book is only reached on the qualified path, so it always books.
        slot = "2026-07-15T10:00:00"
        return ToolResult(
            tool="book",
            status="ok",
            outcome="booked",
            summary=f"Booked a meeting for {lead.name} at {slot}.",
            data={"slot": slot},
        )


# Registration happens at import time; consumers must `import app.providers` to populate the registry.
register_provider("reach", SimulatedReachProvider())
register_provider("qualify", SimulatedQualifyProvider())
register_provider("book", SimulatedBookProvider())

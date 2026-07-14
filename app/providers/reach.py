"""Reach tool providers: a simulated voice call (default) and a real telephony adapter."""

import asyncio
from typing import Any

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider


class SimulatedReachProvider(Provider):
    # Outcome is derived deterministically from lead.sim_profile so the demo
    # reliably exercises each branch; a null profile (real/user-added lead) falls
    # through to the "answered" path.
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(providers.STEP_DELAY_SECONDS)

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


class TwilioReachProvider(CredentialGatedProvider):
    label = "Twilio Voice"
    required_env = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]


register_provider("reach", "sim", SimulatedReachProvider(), label="Simulated", default=True)
register_provider("reach", "twilio", TwilioReachProvider(), label=TwilioReachProvider.label)

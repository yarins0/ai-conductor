"""Reach tool providers: a simulated voice call (default) and a real telephony adapter."""

import asyncio
import os
from typing import Any

import httpx

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

    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        account_sid = os.environ["TWILIO_ACCOUNT_SID"]
        auth_token = os.environ["TWILIO_AUTH_TOKEN"]
        from_number = os.environ["TWILIO_FROM_NUMBER"]
        # Twilio needs TwiML (what the call says/does) at a reachable URL. Falls back to
        # Twilio's own public demo greeting so this works with zero extra setup.
        twiml_url = settings.get("twiml_url", "http://demo.twilio.com/docs/voice.xml")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json",
                auth=(account_sid, auth_token),
                data={"To": lead.phone, "From": from_number, "Url": twiml_url},
            )
        response.raise_for_status()
        call = response.json()

        # ponytail: Twilio calls are async — whether it's actually answered only
        # arrives later via a status-callback webhook, which this project doesn't
        # have an endpoint for. "initiated" reports the call was placed; add a
        # /webhooks/twilio endpoint + look up call.sid there if a synchronous
        # answered/no_answer outcome is needed.
        return ToolResult(
            tool="reach",
            status="ok",
            outcome="initiated",
            summary=f"Placed a real call to {lead.phone} via Twilio (SID {call['sid']}).",
            data={"channel": "voice", "call_sid": call["sid"], "call_status": call["status"]},
        )


register_provider("reach", "sim", SimulatedReachProvider(), label="Simulated", default=True)
register_provider("reach", "twilio", TwilioReachProvider(), label=TwilioReachProvider.label)

"""Reach tool providers: a simulated voice call (default) and a real telephony adapter."""

import asyncio
import os
from typing import Any

import httpx

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app import sim_lead
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider


class SimulatedReachProvider(Provider):
    """Places a call that actually happens: a bounded conversation between the
    assistant (speaking from its spec) and a simulated lead (see app/sim_lead.py).

    `sim_profile` still decides who picks up, so the demo reliably exercises the
    no-answer branch — but what gets *said* once they do is no longer scripted,
    and the transcript rides back in `data` for qualify and the session view.
    """

    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        if lead.sim_profile == "no_answer":
            await asyncio.sleep(providers.STEP_DELAY_SECONDS)  # ringing out
            return ToolResult(
                tool="reach",
                status="ok",
                outcome="no_answer",
                summary=f"Called {lead.phone} — no answer.",
                data={"channel": "voice"},
            )

        # The caller (Runtime / LiveSession) injects the spec, because the
        # assistant on the call *is* the spec. Without one there is nobody to be,
        # and a canned "they picked up" is precisely the lie this replaced — so
        # this reports an error rather than inventing an outcome.
        spec = settings.get("spec")
        if spec is None:
            return ToolResult(
                tool="reach",
                status="error",
                outcome="provider_error",
                summary="Cannot place a call without an assistant spec to speak from.",
                data={"channel": "voice"},
            )

        transcript = await sim_lead.run_call(spec, lead)
        # The summary is what gets fed back to the live agent, so it carries the
        # call itself — otherwise the assistant would be narrating a call it has
        # no way to read.
        return ToolResult(
            tool="reach",
            status="ok",
            outcome="answered",
            summary=(
                f"Called {lead.name} at {lead.phone} — they picked up.\n"
                f"{sim_lead.as_dialogue(transcript)}"
            ),
            data={"channel": "voice", "transcript": transcript},
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

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

        run_id = settings.get("run_id")
        public_base_url = os.getenv("PUBLIC_BASE_URL")
        data = {"To": lead.phone, "From": from_number}
        if run_id and public_base_url:
            # The bridge (app/realtime_bridge.py) needs a run to look up the spec
            # and lead for the call it answers, and the status callback needs one
            # to persist a no-answer/busy/failed outcome — both only reachable
            # once this server itself is reachable from Twilio (PUBLIC_BASE_URL).
            data["Url"] = settings.get("twiml_url", f"{public_base_url}/twilio/voice/{run_id}")
            data["StatusCallback"] = f"{public_base_url}/twilio/status/{run_id}"
            data["StatusCallbackEvent"] = "completed no-answer busy failed"
        else:
            # No run/public URL to bridge through: Twilio's own public demo
            # greeting so this still works with zero extra setup.
            data["Url"] = settings.get("twiml_url", "http://demo.twilio.com/docs/voice.xml")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json",
                auth=(account_sid, auth_token),
                data=data,
            )
        response.raise_for_status()
        call = response.json()

        # Twilio calls are async: "initiated" reports the call was placed. The
        # durable answered/no_answer outcome comes back later via one of two side
        # channels — /twilio/status (busy/no-answer/failed) or /twilio/stream (a
        # live bridge into OpenAI Realtime once someone picks up) — both of which
        # persist the actual reach step this call doesn't have yet.
        return ToolResult(
            tool="reach",
            status="ok",
            outcome="initiated",
            summary=f"Placed a real call to {lead.phone} via Twilio (SID {call['sid']}).",
            data={"channel": "voice", "call_sid": call["sid"], "call_status": call["status"]},
        )


register_provider("reach", "sim", SimulatedReachProvider(), label="Simulated", default=True)
register_provider("reach", "twilio", TwilioReachProvider(), label=TwilioReachProvider.label)

"""Qualify tool providers: a simulated intent score (default) and a real CRM adapter."""

import asyncio
import os
from typing import Any

import anthropic
import httpx

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app import sim_lead
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider

QUALIFY_THRESHOLD = 50  # shared by the simulated scorer and the HubSpot adapter
SCORER_MODEL = "claude-sonnet-5"
SCORER_MAX_OUTPUT_TOKENS = 512
SCORE_TOOL_NAME = "record_intent_score"

_SCORER_SYSTEM_PROMPT = (
    "You score sales-call transcripts for buying intent. Judge only what the lead "
    "actually said — their stated need, urgency, budget, and authority to decide. "
    "Ignore how well the caller performed. Score 0-100: below 50 means this lead "
    "should not be pursued now. Record your score with the record_intent_score tool."
)

# Forced tool use for structured output, mirroring the Builder's create path
# (see app/builder.py) — the same reason applies: it is the reliable way to get a
# typed result back out of the model.
_SCORE_TOOL = {
    "name": SCORE_TOOL_NAME,
    "description": "Record the lead's buying intent as judged from the call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent_score": {
                "type": "integer",
                "description": "Buying intent from 0 (no interest) to 100 (ready to buy).",
            },
            "reason": {
                "type": "string",
                "description": "One sentence citing what the lead said that drove the score.",
            },
        },
        "required": ["intent_score", "reason"],
    },
}


class SimulatedQualifyProvider(Provider):
    """Scores the lead's intent from the call transcript when there is one.

    A transcript only exists if `reach` ran first and reached someone (the caller
    threads it through settings). Without one — qualify called standalone, which
    the live agent may well do — this falls back to the sim_profile-derived score,
    so the tool still works in isolation.
    """

    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        transcript = settings.get("transcript")
        if transcript:
            return await self._score_transcript(lead, transcript)
        return await self._score_from_profile(lead)

    async def _score_transcript(
        self, lead: LeadRecord, transcript: list[dict[str, str]]
    ) -> ToolResult:
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=SCORER_MODEL,
            max_tokens=SCORER_MAX_OUTPUT_TOKENS,
            system=_SCORER_SYSTEM_PROMPT,
            tools=[_SCORE_TOOL],
            tool_choice={"type": "tool", "name": SCORE_TOOL_NAME},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Call transcript with {lead.name} at {lead.company}:\n"
                        f"{sim_lead.as_dialogue(transcript)}"
                    ),
                }
            ],
        )

        block = next((b for b in response.content if b.type == "tool_use"), None)
        if block is None:
            return ToolResult(
                tool="qualify",
                status="error",
                outcome="provider_error",
                summary="The scorer did not return a score for that call.",
            )

        # Boundary validation (implementation rule #2): the model's numbers are
        # untrusted input, and an out-of-range score would corrupt the Brain.
        try:
            intent_score = int(block.input["intent_score"])
            reason = str(block.input["reason"])
        except (KeyError, TypeError, ValueError):
            return ToolResult(
                tool="qualify",
                status="error",
                outcome="provider_error",
                summary="The scorer returned a malformed score for that call.",
            )
        intent_score = max(0, min(100, intent_score))

        outcome = "qualified" if intent_score >= QUALIFY_THRESHOLD else "not_qualified"
        return ToolResult(
            tool="qualify",
            status="ok",
            outcome=outcome,
            summary=f"Qualification score {intent_score} — {outcome.replace('_', ' ')}. {reason}",
            data={"intent_score": intent_score, "reason": reason, "scored_from": "transcript"},
        )

    async def _score_from_profile(self, lead: LeadRecord) -> ToolResult:
        """Fallback for a qualify with no call behind it: score from sim_profile."""
        await asyncio.sleep(providers.STEP_DELAY_SECONDS)

        intent_score = 25 if lead.sim_profile == "not_qualified" else 85
        outcome = "qualified" if intent_score >= QUALIFY_THRESHOLD else "not_qualified"
        return ToolResult(
            tool="qualify",
            status="ok",
            outcome=outcome,
            summary=f"Qualification score {intent_score} — no call to judge; scored from profile.",
            data={"intent_score": intent_score, "scored_from": "profile"},
        )


class HubSpotQualifyProvider(CredentialGatedProvider):
    label = "HubSpot Intent"
    required_env = ["HUBSPOT_API_KEY"]

    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        headers = {"Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}"}

        async with httpx.AsyncClient(headers=headers) as client:
            search = await client.post(
                "https://api.hubapi.com/crm/v3/objects/contacts/search",
                json={
                    "filterGroups": [{"filters": [{"propertyName": "phone", "operator": "EQ", "value": lead.phone}]}],
                    "properties": ["intent_score"],
                },
            )
            search.raise_for_status()
            results = search.json()["results"]

            if results:
                contact = results[0]
            else:
                create = await client.post(
                    "https://api.hubapi.com/crm/v3/objects/contacts",
                    json={"properties": {"phone": lead.phone, "firstname": lead.name, "company": lead.company}},
                )
                create.raise_for_status()
                contact = create.json()

        # ponytail: HubSpot has no built-in "intent score" reachable via a plain
        # API call (real predictive lead scoring is a paid feature). 
        # intent_score must be a custom contact property in your portal (Settings -> Properties
        # -> Contact) to be real; falls back to a fixed qualifying score if unset.
        raw_score = contact.get("properties", {}).get("intent_score")
        intent_score = int(raw_score) if raw_score else 85
        outcome = "qualified" if intent_score >= QUALIFY_THRESHOLD else "not_qualified"

        return ToolResult(
            tool="qualify",
            status="ok",
            outcome=outcome,
            summary=f"HubSpot contact {contact['id']} — intent score {intent_score}.",
            data={"intent_score": intent_score, "hubspot_contact_id": contact["id"]},
        )


register_provider("qualify", "sim", SimulatedQualifyProvider(), label="Simulated", default=True)
register_provider("qualify", "hubspot", HubSpotQualifyProvider(), label=HubSpotQualifyProvider.label)

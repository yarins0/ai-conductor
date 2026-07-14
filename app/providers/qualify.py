"""Qualify tool providers: a simulated intent score (default) and a real CRM adapter."""

import asyncio
import os
from typing import Any

import httpx

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider

HUBSPOT_QUALIFY_THRESHOLD = 50


class SimulatedQualifyProvider(Provider):
    # Score is derived from lead.sim_profile; a null profile qualifies (happy path).
    async def execute(self, lead: LeadRecord, settings: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(providers.STEP_DELAY_SECONDS)

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
        outcome = "qualified" if intent_score >= HUBSPOT_QUALIFY_THRESHOLD else "not_qualified"

        return ToolResult(
            tool="qualify",
            status="ok",
            outcome=outcome,
            summary=f"HubSpot contact {contact['id']} — intent score {intent_score}.",
            data={"intent_score": intent_score, "hubspot_contact_id": contact["id"]},
        )


register_provider("qualify", "sim", SimulatedQualifyProvider(), label="Simulated", default=True)
register_provider("qualify", "hubspot", HubSpotQualifyProvider(), label=HubSpotQualifyProvider.label)

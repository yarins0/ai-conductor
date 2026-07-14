"""Qualify tool providers: a simulated intent score (default) and a real CRM adapter."""

import asyncio
from typing import Any

import app.providers as providers  # STEP_DELAY_SECONDS lives on the package (shared, monkeypatchable)
from app.db import LeadRecord
from app.providers.base import CredentialGatedProvider, Provider, ToolResult, register_provider


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


register_provider("qualify", "sim", SimulatedQualifyProvider(), label="Simulated", default=True)
register_provider("qualify", "hubspot", HubSpotQualifyProvider(), label=HubSpotQualifyProvider.label)

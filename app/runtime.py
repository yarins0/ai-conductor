"""Runtime — drives a spec's tool sequence for a lead and writes every
outcome back to the Company Brain (Context Store).

Importing this module registers the simulated providers (see app/providers.py)
so the Tool Registry is populated as a side effect of import.
"""

from typing import Any

import app.providers  # noqa: F401  (side effect: registers simulated providers)
from app import db
from app.db import LeadRecord
from app.spec import AssistantSpec
from app.tools import ToolResult, get_provider


async def execute_run(run_id: int, spec: AssistantSpec, lead: LeadRecord) -> None:
    intent_score: int | None = None

    for tool in spec.tools:
        provider = get_provider(tool.name)
        if provider is None:
            _fail_run(run_id, tool.name, "unknown_tool", f"No provider registered for tool '{tool.name}'.")
            return

        try:
            result = await provider.execute(lead, tool.settings)
        except Exception as error:  # a provider bug must never crash the server or strand a run
            _fail_run(run_id, tool.name, "provider_error", f"Provider raised: {error}")
            return

        db.add_run_step(run_id, tool.name, result.model_dump_json())

        if result.outcome == "no_answer":
            db.update_lead_outcome(lead.id, "unreachable")
            db.finish_run(run_id, "completed")
            return
        if result.outcome == "not_qualified":
            db.update_lead_outcome(lead.id, "not_qualified", intent_score=result.data.get("intent_score"))
            db.finish_run(run_id, "completed")
            return
        if result.outcome == "qualified":
            intent_score = result.data.get("intent_score")
            # Written immediately (not just at booking) so a spec without a book
            # tool still leaves the qualification result in the Brain (rule #7).
            db.update_lead_outcome(lead.id, "qualified", intent_score=intent_score)
        elif result.outcome == "booked":
            db.update_lead_outcome(lead.id, "booked", intent_score=intent_score, booked_slot=result.data.get("slot"))

    db.finish_run(run_id, "completed")


def _fail_run(run_id: int, tool_name: str, outcome: str, summary: str) -> None:
    error_result = ToolResult(tool=tool_name, status="error", outcome=outcome, summary=summary)
    db.add_run_step(run_id, tool_name, error_result.model_dump_json())
    db.finish_run(run_id, "failed")

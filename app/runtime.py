"""Runtime — drives a spec's tool sequence for a lead and writes every
outcome back to the Company Brain (Context Store).

Importing the app.providers package registers every provider (see
app/providers/ — one module per tool, each with its simulated + real adapter)
so the Tool Registry is populated as a side effect of import.
"""

from typing import Any

from app import db
from app.db import LeadRecord
# Importing the app.providers package also registers every provider as a side effect.
from app.providers import DEFAULT_PROVIDER, ToolResult, get_provider
from app.spec import AssistantSpec, ToolConfig


def _settings_for(
    tool: ToolConfig, spec: AssistantSpec, transcript: list[dict[str, str]] | None
) -> dict[str, Any]:
    """The spec's per-tool settings, plus the run context a tool may need: `spec`
    so a tool can speak as this assistant (reach), and `transcript` so a tool can
    judge what was said (qualify). Both ride the open settings dict, so no
    provider signature changes and providers ignore what they don't know."""
    settings: dict[str, Any] = {**tool.settings, "spec": spec}
    if transcript is not None:
        settings["transcript"] = transcript
    return settings


async def execute_run(run_id: int, spec: AssistantSpec, lead: LeadRecord, providers: dict[str, str] | None = None) -> None:
    intent_score: int | None = None
    transcript: list[dict[str, str]] | None = None

    for tool in spec.tools:
        pid = (providers or {}).get(tool.name)
        provider = get_provider(tool.name, pid)
        if provider is None:
            _fail_run(run_id, tool.name, "unknown_tool", f"No provider registered for tool '{tool.name}'.")
            return

        try:
            result = await provider.execute(lead, _settings_for(tool, spec, transcript))
        except Exception as error:  # a provider bug must never crash the server or strand a run
            _fail_run(run_id, tool.name, "provider_error", f"Provider raised: {error}")
            return

        # Stamp the resolved provider id so the Company Brain shows which adapter ran.
        result.data.setdefault("provider", pid or DEFAULT_PROVIDER.get(tool.name))
        db.add_run_step(run_id, tool.name, result.model_dump_json())

        # Carry a call transcript forward so a later tool can judge what was
        # actually said (qualify scores it) instead of guessing.
        if result.data.get("transcript"):
            transcript = result.data["transcript"]

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

"""Stateless tool execution — one tool call for one lead, persisted.

Extracted from LiveSession._invoke_tool/_write_back when the operator surface
moved to OpenAI Realtime over WebRTC: the browser owns the conversation there,
so the server no longer holds a session object between tool calls. Every call
arrives as a self-contained REST request; the run row and the last call
transcript — the two pieces of state LiveSession kept in memory — are instead
found in the database (`get_or_create_run`, `latest_transcript_for_run`), so a
conversation still accumulates into the Company Brain exactly like a scripted
run.
"""

from typing import Any

from app import db
from app.providers import (
    DEFAULT_PROVIDER,
    ProviderConfigError,
    ToolResult,
    get_provider,
)
from app.spec import AssistantSpec


class ToolNotAllowed(Exception):
    """The requested tool is not in the assistant's spec — the caller maps this
    to a 400. Distinct from an unknown provider (an error ToolResult the model
    can hear and relay), because a tool outside the spec is a client bug, not a
    conversational outcome."""


def write_back_lead_outcome(lead_id: int, result: ToolResult) -> None:
    """Persist a tool outcome to the lead, mirroring the Runtime's write-back so
    the Company Brain looks the same for realtime and scripted runs. Nothing is
    terminal here — a `not_qualified` outcome is recorded but the conversation
    continues; the model decides what happens next."""
    outcome = result.outcome
    intent_score = result.data.get("intent_score")
    if outcome == "no_answer":
        db.update_lead_outcome(lead_id, "unreachable")
    elif outcome == "not_qualified":
        db.update_lead_outcome(lead_id, "not_qualified", intent_score=intent_score)
    elif outcome == "qualified":
        db.update_lead_outcome(lead_id, "qualified", intent_score=intent_score)
    elif outcome == "booked":
        db.update_lead_outcome(
            lead_id, "booked", intent_score=intent_score, booked_slot=result.data.get("slot")
        )
    # "answered" / "initiated" and any other outcome: recorded as a step only.


async def run_tool(
    spec_id: int,
    spec: AssistantSpec,
    lead: db.LeadRecord,
    tool_name: str,
    provider_id: str | None,
    model_args: dict[str, Any],
) -> tuple[int, ToolResult]:
    """Resolve and run one tool for one lead; persist the step and the lead
    write-back to that pair's run row. Returns (run_id, result).

    Never raises for conversational failures — a missing provider, missing
    credentials, or a provider bug all become error ToolResults the voice model
    can hear and relay to the operator. Only a tool outside the spec raises
    (ToolNotAllowed), because that request should never have been made.
    """
    if tool_name not in {tool.name for tool in spec.tools}:
        raise ToolNotAllowed(f"Tool '{tool_name}' is not in this assistant's spec.")

    run = db.get_or_create_run(spec_id, lead.id)

    provider = get_provider(tool_name, provider_id)
    if provider is None:
        result = ToolResult(
            tool=tool_name,
            status="error",
            outcome="unknown_tool",
            summary=f"No provider registered for tool '{tool_name}'.",
        )
        db.add_run_step(run.id, tool_name, result.model_dump_json())
        return run.id, result

    tool_settings = next((tool.settings for tool in spec.tools if tool.name == tool_name), {})
    # `run_id` and `spec` go last so a model-authored argument can never
    # overwrite the run context. Same shape as runtime._settings_for.
    settings: dict[str, Any] = {
        **tool_settings,
        **model_args,
        "run_id": run.id,
        "spec": spec,
    }
    transcript = db.latest_transcript_for_run(run.id)
    if transcript is not None:
        settings["transcript"] = transcript

    try:
        provider.check_credentials()
        result = await provider.execute(lead, settings)
    except ProviderConfigError as error:
        # Missing env vars become a spoken outcome ("Twilio isn't configured"),
        # not a 400 — the operator hears it and can switch providers.
        result = ToolResult(
            tool=tool_name,
            status="error",
            outcome="config_error",
            summary=str(error),
        )
    except Exception as error:  # a provider bug must never strand the conversation
        result = ToolResult(
            tool=tool_name,
            status="error",
            outcome="provider_error",
            summary=f"The {tool_name} tool failed: {error}",
        )

    result.data.setdefault("provider", provider_id or DEFAULT_PROVIDER.get(tool_name))

    # ponytail: a Twilio reach that only *placed* the call (outcome "initiated")
    # is not persisted — the media-stream bridge writes the single durable reach
    # step once the call resolves, so `latest_transcript_for_run` and the UI
    # never see a placeholder. If a "call ringing…" row is ever wanted, persist
    # it here and have the bridge's step supersede it.
    if not (tool_name == "reach" and result.outcome == "initiated"):
        db.add_run_step(run.id, tool_name, result.model_dump_json())
        write_back_lead_outcome(lead.id, result)

    return run.id, result

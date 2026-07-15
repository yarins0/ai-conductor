"""Simulated lead — the second agent on a call.

The `reach` tool's simulated provider used to sleep and return a canned "they
picked up". That made every call a claim with nothing behind it: no conversation
to inspect, and later a qualification score with nothing to score.

This runs the call for real, between two agents: the assistant (driven by its own
spec — the same artifact the Builder wrote) and a lead played by a second model,
whose stance comes from `lead.sim_profile`. It returns the transcript, so what the
assistant actually said is inspectable and `qualify` has real material to judge.

Both sides are plain text loops with a hard exchange cap — no tools. On the call
the assistant only talks; deciding *what to do next* stays with the caller
(`Runtime` or `LiveSession`), which is the layer that owns tool choice.

ponytail: the transcript is returned whole, not streamed, because
`Provider.execute` returns one `ToolResult`. Make execute an async generator if
watching a call unfold live is worth more than the simplicity of that contract.
"""

from typing import Any

import anthropic

from app.call_prompt import lead_call_instructions
from app.db import LeadRecord
from app.spec import AssistantSpec

ASSISTANT_MODEL = "claude-sonnet-5"
# The lead holds a persona and a stance, not reasoning, and a call costs two model
# round-trips per exchange — so the cheap model goes on this side.
LEAD_MODEL = "claude-haiku-4-5-20251001"
MAX_OUTPUT_TOKENS = 300  # phone turns, not documents
MAX_CALL_EXCHANGES = 4

# How the lead behaves once they pick up, keyed by sim_profile. This is the same
# knob the old canned provider keyed its fake outcome off — the demo stays
# deterministic in who does what, but it is now expressed as behavior rather than
# as a hardcoded return value. A null profile (any lead added through the API)
# gets the neutral persona.
_LEAD_PERSONAS = {
    "books": (
        "You have a real, current problem in the caller's area and budget to fix "
        "it this quarter. Start a little guarded, warm up once they say something "
        "relevant, and agree to a meeting if they ask for one."
    ),
    "not_qualified": (
        "You are polite but genuinely the wrong fit: no budget this year, and the "
        "decision is not yours to make anyway. Do not fake interest you do not "
        "have, and do not agree to a meeting."
    ),
}
_NEUTRAL_LEAD_PERSONA = (
    "You are mildly guarded and short on time. Engage if the caller says something "
    "relevant to your work, stay non-committal otherwise."
)

# The assistant speaks first, as on a real outbound call, so the lead needs a line
# to have answered with. Scripted rather than generated — it is a pickup, not a
# conversation, and the model API needs a first message to reply to.
_PICKUP_LINE = "Hello?"


def _lead_system_prompt(lead: LeadRecord) -> str:
    """The lead's side. Stance comes from sim_profile; everything else is just
    instructions to sound like a person on a phone rather than an assistant."""
    return "\n".join(
        [
            f"You are {lead.name}, who works at {lead.company}. You just picked up "
            "an unexpected sales call from someone you do not know.",
            _LEAD_PERSONAS.get(lead.sim_profile or "", _NEUTRAL_LEAD_PERSONA),
            "You are a busy human, not an assistant. Talk the way people actually "
            "talk on the phone: one or two sentences, sometimes clipped. Never "
            "write stage directions and never break character.",
        ]
    )


async def _say(client: Any, model: str, system: str, messages: list[dict[str, Any]]) -> str:
    """One spoken turn from one side of the call."""
    response = await client.messages.create(
        model=model, max_tokens=MAX_OUTPUT_TOKENS, system=system, messages=messages
    )
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # An empty turn would be rejected as the next message's content, stranding the
    # call; a filler beat keeps the loop alive and reads as a real pause.
    return text or "..."


async def run_call(spec: AssistantSpec, lead: LeadRecord) -> list[dict[str, str]]:
    """Run a bounded assistant <-> lead call and return the transcript as an
    ordered list of {"speaker": "assistant" | "lead", "text": ...}.

    Each side keeps its own message history, mirrored: what one says as
    `assistant` in its own history arrives as `user` in the other's. That mirroring
    is what makes this two agents talking to each other, rather than one model
    writing both parts of a screenplay.
    """
    client = anthropic.AsyncAnthropic()
    assistant_system = lead_call_instructions(spec, lead, MAX_CALL_EXCHANGES)
    lead_system = _lead_system_prompt(lead)

    transcript: list[dict[str, str]] = [{"speaker": "lead", "text": _PICKUP_LINE}]
    assistant_messages: list[dict[str, Any]] = [{"role": "user", "content": _PICKUP_LINE}]
    lead_messages: list[dict[str, Any]] = []

    for _ in range(MAX_CALL_EXCHANGES):
        assistant_text = await _say(client, ASSISTANT_MODEL, assistant_system, assistant_messages)
        transcript.append({"speaker": "assistant", "text": assistant_text})
        assistant_messages.append({"role": "assistant", "content": assistant_text})
        lead_messages.append({"role": "user", "content": assistant_text})

        lead_text = await _say(client, LEAD_MODEL, lead_system, lead_messages)
        transcript.append({"speaker": "lead", "text": lead_text})
        lead_messages.append({"role": "assistant", "content": lead_text})
        assistant_messages.append({"role": "user", "content": lead_text})

    return transcript

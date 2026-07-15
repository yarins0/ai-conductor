"""Simulated-call tests.

These exercise the call loop's own mechanics — turn alternation, the exchange
cap, the message mirroring that makes this two agents rather than one model
writing both parts, and the persona wiring — against a faked Anthropic client,
so they cost nothing and are deterministic. Whether the real models hold a
*convincing* conversation is a model-quality question, answered in the demo.
"""

import asyncio
from datetime import datetime, timezone

from conftest import Response, text_block

from app import sim_lead
from app.db import LeadRecord
from app.spec import AssistantSpec, ToolConfig

SAMPLE_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Qualify the lead and book a meeting.",
    persona="Friendly and direct.",
    instructions=["Never promise a discount."],
    tools=[ToolConfig(name="reach")],
)


def _make_lead(sim_profile: str | None) -> LeadRecord:
    now = datetime.now(timezone.utc)
    return LeadRecord(
        name="Jane Prospect",
        company="Acme Co",
        phone="555-0100",
        sim_profile=sim_profile,
        created_at=now,
        updated_at=now,
    )


def _numbered_turns(count: int = 20) -> list[Response]:
    return [Response([text_block(f"turn {index}")]) for index in range(count)]


def test_call_alternates_speakers_and_respects_the_exchange_cap(fake_anthropic) -> None:
    client = fake_anthropic(_numbered_turns())

    transcript = asyncio.run(sim_lead.run_call(SAMPLE_SPEC, _make_lead("books")))

    # The pickup line, then one assistant turn and one lead turn per exchange.
    assert [entry["speaker"] for entry in transcript] == (
        ["lead"] + ["assistant", "lead"] * sim_lead.MAX_CALL_EXCHANGES
    )
    # The cap is a real bound on cost, not just on the transcript length.
    assert client.messages.calls == 2 * sim_lead.MAX_CALL_EXCHANGES


def test_each_side_hears_the_other(fake_anthropic) -> None:
    # The mirroring is the whole point: what one side says as `assistant` in its
    # own history must arrive as `user` in the other's, or they are not talking.
    client = fake_anthropic(_numbered_turns())

    asyncio.run(sim_lead.run_call(SAMPLE_SPEC, _make_lead("books")))

    assistant_opening, lead_reply = client.messages.requests[0], client.messages.requests[1]
    assert assistant_opening["model"] == sim_lead.ASSISTANT_MODEL
    assert lead_reply["model"] == sim_lead.LEAD_MODEL
    # The lead's first turn hears the assistant's opening ("turn 0") as user input.
    assert lead_reply["messages"] == [{"role": "user", "content": "turn 0"}]
    # The assistant's second turn hears the lead's reply ("turn 1") back.
    assert client.messages.requests[2]["messages"][-1] == {"role": "user", "content": "turn 1"}


def test_assistant_speaks_from_the_spec(fake_anthropic) -> None:
    client = fake_anthropic(_numbered_turns())

    asyncio.run(sim_lead.run_call(SAMPLE_SPEC, _make_lead("books")))

    system = client.messages.requests[0]["system"]
    assert SAMPLE_SPEC.name in system
    assert SAMPLE_SPEC.objective in system
    assert "Never promise a discount." in system


def test_lead_stance_comes_from_sim_profile(fake_anthropic) -> None:
    client = fake_anthropic(_numbered_turns())

    asyncio.run(sim_lead.run_call(SAMPLE_SPEC, _make_lead("not_qualified")))

    assert "no budget this year" in client.messages.requests[1]["system"]


def test_null_profile_lead_gets_the_neutral_stance(fake_anthropic) -> None:
    # Any lead added through the API has no profile; it must still have a persona.
    client = fake_anthropic(_numbered_turns())

    asyncio.run(sim_lead.run_call(SAMPLE_SPEC, _make_lead(None)))

    assert sim_lead._NEUTRAL_LEAD_PERSONA in client.messages.requests[1]["system"]


def test_empty_turn_becomes_a_placeholder(fake_anthropic) -> None:
    # An empty string would be rejected as the next message's content and strand
    # the call, so it must never reach the transcript.
    fake_anthropic([Response([text_block("   ")])] + _numbered_turns())

    transcript = asyncio.run(sim_lead.run_call(SAMPLE_SPEC, _make_lead("books")))

    assert transcript[1] == {"speaker": "assistant", "text": "..."}

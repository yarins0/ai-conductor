"""The shared on-call prompt (app/call_prompt.py).

These lock a live regression. Every spec the Builder writes states its objective
in the operator's idiom — the real ones on disk open "Place a warm, friendly
outbound call to a lead...", "Call each lead...", "Reach, qualify, book." —
because that is the whole job it was asked to describe, the reach step included.
Both call surfaces then handed that to the model as "Your objective on this
call", so an assistant already talking to Dana read its objective as: call Dana.
It opened the call by announcing "calling Dana, placing the call now" — to Dana,
who had already picked up. The fix is scoping, so the assertions are about
scoping, not wording.
"""

from datetime import datetime, timezone

from app.call_prompt import as_dialogue, lead_call_instructions
from app.db import LeadRecord
from app.spec import AssistantSpec, ToolConfig

# Verbatim shape of a real stored spec, operator idiom and tool references intact.
SPEC = AssistantSpec(
    name="Warm Outreach & Booking Assistant",
    objective=(
        "Place a warm, friendly outbound call to a lead, qualify their interest, "
        "and book a meeting on their behalf."
    ),
    persona="Warm, friendly, and encouraging.",
    instructions=[
        "Greet the lead warmly and personably, using their name if available.",
        "Score the lead's intent using the qualify tool based on the conversation.",
        "If the lead is qualified, use the book tool to set up a convenient time.",
    ],
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)


def _lead(notes: str | None = None) -> LeadRecord:
    now = datetime.now(timezone.utc)
    return LeadRecord(
        name="Dana",
        company="Acme Co",
        phone="555-0100",
        notes=notes,
        created_at=now,
        updated_at=now,
    )


def test_objective_reaches_the_model_but_never_as_this_call_s_task() -> None:
    prompt = lead_call_instructions(SPEC, _lead())

    # The objective still gets through — it is the spec, and the spec is the
    # source of truth. What changed is the frame around it.
    assert SPEC.objective in prompt
    assert "your objective on this call" not in prompt.lower()
    assert "of which this call is one step" in prompt


def test_prompt_says_the_call_is_already_connected() -> None:
    """The specific confusion: treating the person on the line as the operator
    to announce a call to, rather than as the lead already on it."""
    prompt = lead_call_instructions(SPEC, _lead())

    assert "already connected" in prompt
    assert "Dana from Acme Co picked up" in prompt
    assert "never your operator" in prompt


def test_instructions_are_carried_but_scoped_to_the_conversation() -> None:
    prompt = lead_call_instructions(SPEC, _lead())

    for instruction in SPEC.instructions:
        assert f"- {instruction}" in prompt
    # Specs name tools the call surface does not hand the model; without saying
    # so, obeying "use the book tool" can only mean narrating using it.
    assert "You have no tools on this call." in prompt


def test_lead_notes_are_included_only_when_present() -> None:
    assert "Notes on this lead: Met at a conference." in lead_call_instructions(
        SPEC, _lead(notes="Met at a conference.")
    )
    assert "Notes on this lead:" not in lead_call_instructions(SPEC, _lead())


def test_turn_budget_is_the_simulator_s_alone() -> None:
    """The simulator caps exchanges; a real phone call does not, and must not be
    told it has 'roughly 4 exchanges' left."""
    assert "roughly 4 exchanges" in lead_call_instructions(SPEC, _lead(), 4)
    assert "exchanges before the call ends" not in lead_call_instructions(SPEC, _lead())


def test_as_dialogue_flattens_speaker_turns() -> None:
    transcript = [
        {"speaker": "lead", "text": "Hello?"},
        {"speaker": "assistant", "text": "Hi Dana."},
    ]

    assert as_dialogue(transcript) == "lead: Hello?\nassistant: Hi Dana."

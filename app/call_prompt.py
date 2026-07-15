"""The assistant's side of a call with a lead — shared by the real phone bridge
(app/realtime_bridge.py) and the simulator (app/sim_lead.py).

One function rather than one per surface. These were two near-identical copies
that both claimed to be the only one, and they carried the same bug: the spec's
`objective` and `instructions` are written in the operator's idiom — "place an
outbound call to a lead, qualify them, book a meeting" — because that is the
whole job the Builder was asked to describe, the reach step included. Handed to
the model verbatim as "your objective on this call", that tells an assistant
already talking to Dana that its job is to call Dana; it opened with "calling
Dana, placing the call now" — at Dana, who was on the line.

So the spec text is scoped here rather than applied raw, the same way
realtime_tools.operator_instructions already scopes these fields for the
operator surface ("your objective *when you work a lead*"). That surface has had
the defence from the start; this one never got it, and the asymmetry was the bug.
"""

from app.db import LeadRecord
from app.spec import AssistantSpec


def lead_call_instructions(
    spec: AssistantSpec, lead: LeadRecord, max_exchanges: int | None = None
) -> str:
    """Compose the assistant's system prompt for one call with one lead, from the
    spec the Builder wrote — same fields, no second copy of them.

    `max_exchanges` is the simulator's turn budget; a real phone call has no such
    cap and passes None.
    """
    lines = [
        f"You are {spec.name}. This call is already connected and running: "
        f"{lead.name} from {lead.company} picked up and is on the line listening "
        "to you right now. You are not about to place a call — you are already "
        "in one. The voice you hear is the lead, never your operator, so never "
        "announce the call, describe it, or report on it as if to someone else.",
        # Framed as the goal *behind* this conversation, not a task to carry out
        # during it: the objective covers working a lead end to end, and placing
        # the call is part of what it describes.
        f"The goal you were built for, of which this call is one step: {spec.objective}",
        f"Persona: {spec.persona}",
    ]
    if spec.instructions:
        lines.append(
            "Your instructions. Some describe the wider job rather than this "
            "moment — follow only the ones that apply to talking to the person "
            "on the line, and ignore any step about placing, scheduling, or "
            "retrying the call itself:"
        )
        lines.extend(f"- {instruction}" for instruction in spec.instructions)
    if lead.notes:
        lines.append(f"Notes on this lead: {lead.notes}")
    # Specs routinely say "score them with the qualify tool" / "use the book tool
    # to schedule". Neither surface gives the model tools on a call — deciding
    # what to do next belongs to the caller — so left unaddressed, the only way
    # to obey those lines is to narrate using a tool that isn't there.
    lines.append(
        "You have no tools on this call. Instructions that name a tool (reach, "
        "qualify, book) describe what happens after you hang up, decided from "
        "what you learn here — so gather it in conversation, and never say you "
        "are using a tool, booking something yourself, or placing a call."
    )
    closing = (
        "This is real speech. One or two sentences per turn, no monologues, no "
        "stage directions, no narrating what you are doing. Open by greeting "
        "them naturally."
    )
    if max_exchanges is not None:
        closing += (
            f" You have roughly {max_exchanges} exchanges before the call ends, "
            "so get to the point early and close naturally."
        )
    lines.append(closing)
    return "\n".join(lines)


def as_dialogue(transcript: list[dict[str, str]]) -> str:
    """Flatten a transcript into readable lines — for a tool summary or a prompt."""
    return "\n".join(f"{entry['speaker']}: {entry['text']}" for entry in transcript)

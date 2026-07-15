"""LLM-facing tool definitions for the OpenAI Realtime operator session.

Successor to `tools_manifest.py` (which spoke Anthropic's schema for the Claude
live loop): same idea, OpenAI's function shape. Hand-written for the same reason
as before — a provider's `execute(lead, settings)` takes an open settings dict,
so there is no per-tool argument schema to derive from; the parameters here are
the few things the model may author from the conversation, merged into
`settings` at call time.

Beyond the spec's own tools, every session gets three universal functions:
`list_leads` and `request_lead` exist because the operator picks a lead
mid-conversation now (there is no lead dropdown anymore), and `web_search` is
bridged to OpenAI's Responses API because the Realtime API has function calling
but not the built-in web_search tool.
"""

from datetime import datetime
from typing import Any

from app.spec import AssistantSpec

# tool name -> OpenAI Realtime function definition. Parameters are optional and
# flow into the provider's settings dict; the simulated providers ignore them,
# real ones (e.g. Twilio's twiml_url) can read them.
REALTIME_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "reach": {
        "type": "function",
        "name": "reach",
        "description": (
            "Place an outbound voice call to the lead. Use when the conversation "
            "calls for reaching the lead on the phone. The call runs on its own; "
            "if the result says the call was initiated, tell the operator it is "
            "in progress — the transcript arrives when the call ends."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "twiml_url": {
                    "type": "string",
                    "description": "Optional URL for call instructions (real telephony only).",
                },
                "lead_id": {
                    "type": "integer",
                    "description": "The id of the lead to act on, from list_leads.",
                },
            },
        },
    },
    "qualify": {
        "type": "function",
        "name": "qualify",
        "description": (
            "Score the lead's intent / qualification. Use when you need to judge "
            "whether the lead is a good fit before booking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "notes": {
                    "type": "string",
                    "description": "Optional qualification notes gathered from the conversation.",
                },
                "lead_id": {
                    "type": "integer",
                    "description": "The id of the lead to act on, from list_leads.",
                },
            },
        },
    },
    "book": {
        "type": "function",
        "name": "book",
        "description": (
            "Schedule a meeting with the lead. Use when the lead wants to book "
            "time or has agreed to a meeting."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "preferred_time": {
                    "type": "string",
                    "description": (
                        "The agreed meeting time as an ISO 8601 timestamp with a UTC "
                        "offset, e.g. 2026-07-16T09:00:00+03:00. Resolve relative "
                        "wording like 'tomorrow at 9' against the current date and "
                        "time given in your instructions — never send the words "
                        "themselves. Omit only if no time was actually agreed."
                    ),
                },
                "lead_id": {
                    "type": "integer",
                    "description": "The id of the lead to act on, from list_leads.",
                },
            },
        },
    },
}

# Universal functions, present in every session regardless of spec — they serve
# the conversation itself, not lead work, so the spec doesn't gate them.
LIST_LEADS: dict[str, Any] = {
    "type": "function",
    "name": "list_leads",
    "description": (
        "Search the known leads by name or company. Use this to resolve which "
        "lead the operator means (e.g. 'call Dana from Acme') before acting on "
        "one. Returns matching leads with their ids. The operator already sees "
        "every match as a card in their view, so never read the leads out loud "
        "— do not recite their names, companies, statuses or ids, and do not "
        "summarise the list. This is the one exception to reporting what a tool "
        "returned. If the operator only asked to see their leads, say something "
        "brief like 'here they are' and stop; otherwise say nothing about the "
        "list and carry straight on with what they actually asked for."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Name or company fragment to search for. Empty lists every lead.",
            }
        },
        "required": ["query"],
    },
}

REQUEST_LEAD: dict[str, Any] = {
    "type": "function",
    "name": "request_lead",
    "description": (
        "Ask the operator to pick a lead. Use when you need a lead to act on and "
        "cannot resolve one from the conversation or from list_leads — for "
        "example when several leads share a name. A picker appears in the "
        "operator's view and this call returns the lead they pick, so do not ask "
        "them to say their choice out loud and do not call this again while "
        "waiting. Never guess a lead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Optional name or company fragment to narrow the picker to the "
                    "candidates in question. Omit to show every lead."
                ),
            }
        },
    },
}

WEB_SEARCH: dict[str, Any] = {
    "type": "function",
    "name": "web_search",
    "description": (
        "Search the web for current information — company news, funding, people. "
        "Use when the operator asks about something you wouldn't know offhand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."}
        },
        "required": ["query"],
    },
}


def realtime_tools_for_spec(spec: AssistantSpec) -> list[dict[str, Any]]:
    """The function definitions for exactly the tools the spec allows (in spec
    order), plus the three universal conversation functions. A spec tool with no
    known definition (an unregistered future tool) is skipped rather than
    raising — the session simply can't invoke what it can't describe."""
    spec_tools = [
        REALTIME_TOOL_DEFINITIONS[tool.name]
        for tool in spec.tools
        if tool.name in REALTIME_TOOL_DEFINITIONS
    ]
    return spec_tools + [LIST_LEADS, REQUEST_LEAD, WEB_SEARCH]


def operator_instructions(spec: AssistantSpec) -> str:
    """Compose the operator-facing session instructions from the spec — the same
    fields the Builder writes, no second schema. Adapted from the retired
    live_agent._operator_system_prompt: the spec's objective and instructions
    describe lead work, so they are scoped as such — applied verbatim, they make
    the assistant pitch its own operator."""
    lines = [
        f"You are {spec.name}, speaking with your operator — the person who "
        "configured you. Your operator is not a lead, and you are not on a call "
        "with a prospect right now.",
        f"Your objective when you work a lead: {spec.objective}",
        f"Persona: {spec.persona}",
    ]
    if spec.instructions:
        lines.append("Your instructions when working a lead:")
        lines.extend(f"- {instruction}" for instruction in spec.instructions)
    lines.append(
        "Your tools act on the lead for your operator: reaching them, judging "
        "their intent, booking time. Invoke them when the conversation calls for "
        "it, in whatever order makes sense — do not follow a fixed script. "
        "Resolve which lead the operator means with list_leads; if you cannot "
        "resolve one, call request_lead so the operator can pick — never guess. "
        "You are speaking out loud, so keep replies short, natural, and "
        "conversational. Report what your tools actually returned, including "
        "what was said on a call, and never claim a tool's result before you "
        "have actually called it. Pass the lead_id you resolved via list_leads "
        "when calling reach, qualify, or book."
    )
    # A scheduling assistant that does not know today's date cannot resolve
    # "tomorrow at 9", and the model has no clock of its own. Local time with an
    # offset, not UTC: the operator says "9am" meaning their own 9am, and book
    # wants that back as an exact instant. Frozen for the session like the rest
    # of these instructions — a session still running tomorrow would resolve
    # "tomorrow" against the wrong day, which reconnecting fixes.
    lines.append(
        f"The current date and time is {datetime.now().astimezone().isoformat(timespec='minutes')}."
    )
    return "\n".join(lines)

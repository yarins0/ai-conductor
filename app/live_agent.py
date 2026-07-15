"""Live agent — the non-linear execution mode (Phase 6).

Where the Runtime (`app/runtime.py`) drives a fixed reach -> qualify -> book
sequence per lead, the live agent runs a two-way conversation and lets the model
decide which registered tool to invoke, and when, from open-ended user speech.
It is the flexible layer over the reliable scripted spine; the Runtime is left
untouched.

Structure mirrors the Builder's edit loop (`builder.edit_spec`): a bounded
tool-calling loop over the Anthropic SDK, with tool results fed back so the model
can narrate outcomes and choose the next action, and a hard iteration cap so a
misbehaving model can never spin forever. Each tool call reuses the exact same
provider primitive as the Runtime (`get_provider(...).execute(lead, settings)`)
and writes its step + lead outcome back to the Company Brain, so a live
conversation accumulates identically to a scripted run.

Events yielded per turn (consumed by the WebSocket handler in `main.py`):
- {"type": "assistant", "text": ...}  -> spoken by the browser (TTS)
- {"type": "action", "tool": ..., "result": {...}}  -> a tool was invoked
- {"type": "error", "message": ...}  -> the LLM call failed this turn
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app import db
from app.providers import DEFAULT_PROVIDER, ToolResult, get_provider
from app.spec import AssistantSpec
from app.tools_manifest import tools_for_spec

LIVE_AGENT_MODEL = "claude-sonnet-5"
# Voice replies are short spoken turns, not documents — a small cap keeps latency
# down and discourages the model from monologuing.
MAX_OUTPUT_TOKENS = 1024
# Tool round-trips allowed within a single user turn before we force the turn to
# end. Matches the Builder's edit-loop cap: pure safety headroom, not the normal
# exit (a turn typically resolves in 1-2 iterations).
MAX_TURN_ITERATIONS = 8


def _operator_system_prompt(spec: AssistantSpec) -> str:
    """Compose the operator-facing system prompt from the spec — the same fields
    the Builder writes, no second schema. `tools` is not described here; the tool
    definitions are passed to the API separately (single source of truth).

    Named for its audience, because there are now two conversations this same
    assistant can be in and they must not be conflated: here it is talking *to its
    operator* about leads and acting through tools, whereas
    `sim_lead._assistant_system_prompt` is it talking *to a lead* on a call. The
    spec's objective and instructions describe lead work, so they are scoped as
    such — applied verbatim, they make the assistant pitch its own operator.
    """
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
        "it, in whatever order makes sense — do not follow a fixed script. You "
        "are speaking out loud, so keep replies short, natural, and "
        "conversational. Report what your tools actually returned, including what "
        "was said on a call, and never claim a tool's result before you have "
        "actually called it."
    )
    return "\n".join(lines)


def _write_back(lead_id: int, result: ToolResult) -> None:
    """Persist a tool outcome to the lead, mirroring the Runtime's write-back so
    the Company Brain looks the same for live and scripted runs. Unlike the
    Runtime, nothing is terminal here — a `not_qualified` outcome is recorded but
    the conversation continues; the model decides what happens next."""
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


class LiveSession:
    """One live conversation. Holds the persistent message history, the run row
    its actions persist to, and the per-tool provider + settings selection."""

    def __init__(
        self,
        spec: AssistantSpec,
        lead: db.LeadRecord,
        run_id: int,
        providers: dict[str, str] | None = None,
    ) -> None:
        self.spec = spec
        self.lead = lead
        self.run_id = run_id
        self.providers = providers or {}
        # Per-tool spec settings, merged with model-authored args at call time.
        self.tool_settings = {tool.name: tool.settings for tool in spec.tools}
        self.system_prompt = _operator_system_prompt(spec)
        self.tools = tools_for_spec(spec)
        self.messages: list[dict[str, Any]] = []
        # The most recent call transcript, carried across tools so qualify can
        # score what was actually said (mirrors how the Runtime threads it).
        self.last_transcript: list[dict[str, str]] | None = None
        self.client = anthropic.AsyncAnthropic()

    async def handle_turn(self, user_text: str) -> AsyncIterator[dict[str, Any]]:
        """Run one user turn to completion: call the model, speak its text, invoke
        any tools it chose, feed the results back, and repeat until it stops
        calling tools (or the iteration cap trips). Never raises into the caller —
        an LLM failure becomes an error event, a provider failure becomes a
        recorded error outcome fed back to the model."""
        self.messages.append({"role": "user", "content": user_text})

        for _ in range(MAX_TURN_ITERATIONS):
            try:
                response = await self.client.messages.create(
                    model=LIVE_AGENT_MODEL,
                    max_tokens=MAX_OUTPUT_TOKENS,
                    system=self.system_prompt,
                    tools=self.tools,
                    messages=self.messages,
                )
            except anthropic.APIError:
                yield {"type": "error", "message": "The assistant had trouble responding."}
                return

            self.messages.append({"role": "assistant", "content": response.content})
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    yield {"type": "assistant", "text": block.text}

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                return  # turn done — wait for the next user utterance

            tool_results = []
            for block in tool_uses:
                result = await self._invoke_tool(block.name, block.input or {})
                yield {"type": "action", "tool": result.tool, "result": json.loads(result.model_dump_json())}
                # Feed the human-readable summary back so the model can confirm it.
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result.summary}
                )
            self.messages.append({"role": "user", "content": tool_results})

        # Iteration cap hit without the model settling — end the turn cleanly.
        yield {"type": "assistant", "text": "Let me pause there — what would you like to do next?"}

    async def _invoke_tool(self, tool_name: str, model_args: dict[str, Any]) -> ToolResult:
        """Resolve and run one tool, persist the step + lead write-back, and return
        the typed result. A provider bug becomes an error `ToolResult` (fed back to
        the model) rather than crashing the turn — the Runtime's guarantee, kept."""
        provider_id = self.providers.get(tool_name)
        provider = get_provider(tool_name, provider_id)
        if provider is None:
            return ToolResult(
                tool=tool_name,
                status="error",
                outcome="unknown_tool",
                summary=f"No provider registered for tool '{tool_name}'.",
            )

        # `spec` and `transcript` go last so a model-authored argument can never
        # overwrite the run context. See runtime._settings_for for the same shape.
        settings: dict[str, Any] = {
            **self.tool_settings.get(tool_name, {}),
            **model_args,
            "spec": self.spec,
        }
        if self.last_transcript is not None:
            settings["transcript"] = self.last_transcript

        try:
            result = await provider.execute(self.lead, settings)
        except Exception as error:  # a provider bug must never strand the conversation
            result = ToolResult(
                tool=tool_name,
                status="error",
                outcome="provider_error",
                summary=f"The {tool_name} tool failed: {error}",
            )

        if result.data.get("transcript"):
            self.last_transcript = result.data["transcript"]

        result.data.setdefault("provider", provider_id or DEFAULT_PROVIDER.get(tool_name))
        db.add_run_step(self.run_id, tool_name, result.model_dump_json())
        _write_back(self.lead.id, result)
        return result

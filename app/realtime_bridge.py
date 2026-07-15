"""Twilio Media Streams <-> OpenAI Realtime bridge (Surface B): the phone-call
side of the assistant. Twilio's Media Streams speak G.711 mu-law 8kHz natively;
OpenAI Realtime accepts/emits the same format (`g711_ulaw`) when asked — so the
bridge is a byte-for-byte base64 relay, no audio conversion library needed.

Sibling to app/realtime.py (the operator's WebRTC control plane, which never
touches audio server-side): this surface is nothing BUT audio relay, because a
phone call has no browser to hold the other end.
"""

import asyncio
import json
import os
from typing import Any
from urllib.parse import parse_qs

import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from app import db, tool_exec
from app.providers import ToolResult
from app.realtime import REALTIME_MODEL
from app.sim_lead import as_dialogue
from app.spec import AssistantSpec

router = APIRouter()  # Twilio-facing webhook/stream paths — no /api prefix

# Injectable seam for tests, same monkeypatch style as anthropic.AsyncAnthropic.
connect_fn = websockets.connect

OPENAI_REALTIME_WS_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
# GA field name for streaming input transcription — the one drift-risk here if
# OpenAI renames it again (confirmed against developers.openai.com/api/docs/guides/realtime).
TRANSCRIPTION_MODEL = "gpt-realtime-whisper"
FAILED_CALL_STATUSES = {"busy", "no-answer", "failed", "canceled"}


def twiml_for_run(run_id: int) -> str:
    # Twilio's <Stream> needs wss:// regardless of the scheme PUBLIC_BASE_URL is
    # configured with, so the scheme (if any) is stripped and replaced.
    base = os.getenv("PUBLIC_BASE_URL", "")
    host = base.split("://", 1)[-1]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Connect><Stream url="wss://{host}/twilio/stream/{run_id}"/></Connect></Response>'
    )


@router.post("/twilio/voice/{run_id}")
def twilio_voice(run_id: int) -> Response:
    return Response(content=twiml_for_run(run_id), media_type="application/xml")


@router.post("/twilio/status/{run_id}")
async def twilio_status(run_id: int, request: Request) -> Response:
    """Twilio's status callback — the answered/no_answer distinction that a
    Calls.json POST can't give synchronously (see the ponytail comment in
    app/providers/reach.py). Only a terminal non-connect status writes a step;
    everything else (ringing, in-progress, completed — the bridge itself
    persists that one) is a no-op 200."""
    # python-multipart isn't a project dependency, so this reads the raw
    # x-www-form-urlencoded body directly rather than via Request.form().
    fields = parse_qs((await request.body()).decode())
    call_status = fields.get("CallStatus", [""])[0]
    if call_status in FAILED_CALL_STATUSES:
        run = db.get_run(run_id)
        if run is not None:
            result = ToolResult(
                tool="reach",
                status="ok",
                outcome="no_answer",
                summary=f"Twilio call ended without connecting (status: {call_status}).",
                data={"channel": "voice"},
            )
            db.add_run_step(run_id, "reach", result.model_dump_json())
            tool_exec.write_back_lead_outcome(run.lead_id, result)
    return Response(status_code=200)


def lead_call_instructions(spec: AssistantSpec, lead: db.LeadRecord) -> str:
    """The assistant's side of a live phone call — same shape as
    sim_lead._assistant_system_prompt (same spec fields, no second copy of
    them), plus whatever notes were recorded on this lead."""
    lines = [
        f"You are {spec.name}, on a live outbound phone call you placed to "
        f"{lead.name} at {lead.company}.",
        f"Your objective on this call: {spec.objective}",
        f"Persona: {spec.persona}",
    ]
    if spec.instructions:
        lines.append("Follow these instructions:")
        lines.extend(f"- {instruction}" for instruction in spec.instructions)
    if lead.notes:
        lines.append(f"Notes on this lead: {lead.notes}")
    lines.append(
        "This is real speech over the phone. One or two sentences per turn, no "
        "monologues, no stage directions, no narrating what you are doing."
    )
    return "\n".join(lines)


def _session_update(spec: AssistantSpec, lead: db.LeadRecord) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "instructions": lead_call_instructions(spec, lead),
            "audio": {
                "input": {"format": "g711_ulaw", "transcription": {"model": TRANSCRIPTION_MODEL}},
                "output": {"format": "g711_ulaw"},
            },
        },
    }


def accumulate_transcript(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Pure, out of the socket loop so it's unit-testable: turn a raw OpenAI
    Realtime event stream into the exact {"speaker","text"} shape
    sim_lead.run_call returns, so qualify scores a real call unchanged."""
    transcript: list[dict[str, str]] = []
    for event in events:
        event_type = event.get("type")
        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript.append({"speaker": "lead", "text": event.get("transcript", "")})
        elif event_type == "response.output_audio_transcript.done":
            transcript.append({"speaker": "assistant", "text": event.get("transcript", "")})
    return transcript


async def _bridge_call(
    twilio_ws: Any, spec: AssistantSpec, lead: db.LeadRecord
) -> tuple[list[dict[str, str]], str | None]:
    """Relay G.711 mu-law audio between one Twilio Media Stream and one OpenAI
    Realtime session — no conversion, both ends already speak g711_ulaw. Returns
    the accumulated transcript and the Twilio call SID once the call ends."""
    state: dict[str, Any] = {"stream_sid": None, "call_sid": None}
    raw_events: list[dict[str, Any]] = []

    async with connect_fn(
        OPENAI_REALTIME_WS_URL,
        additional_headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
    ) as openai_ws:
        await openai_ws.send(json.dumps(_session_update(spec, lead)))

        async def relay_twilio_to_openai() -> None:
            while True:
                try:
                    message = await twilio_ws.receive_json()
                except WebSocketDisconnect:
                    return
                event = message.get("event")
                if event == "start":
                    state["stream_sid"] = message["start"]["streamSid"]
                    state["call_sid"] = message["start"].get("callSid")
                elif event == "media":
                    await openai_ws.send(json.dumps(
                        {"type": "input_audio_buffer.append", "audio": message["media"]["payload"]}
                    ))
                elif event == "stop":
                    return

        async def relay_openai_to_twilio() -> None:
            async for raw in openai_ws:
                event = json.loads(raw)
                raw_events.append(event)
                event_type = event.get("type")
                if event_type == "response.output_audio.delta" and state["stream_sid"]:
                    await twilio_ws.send_json({
                        "event": "media",
                        "streamSid": state["stream_sid"],
                        "media": {"payload": event["delta"]},
                    })
                elif event_type == "input_audio_buffer.speech_started" and state["stream_sid"]:
                    # Barge-in: the lead started talking — clear Twilio's queued
                    # assistant audio so the two voices don't talk over each other.
                    await twilio_ws.send_json({"event": "clear", "streamSid": state["stream_sid"]})

        to_openai = asyncio.create_task(relay_twilio_to_openai())
        to_twilio = asyncio.create_task(relay_openai_to_twilio())
        done, pending = await asyncio.wait({to_openai, to_twilio}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            task.result()  # re-raise so a relay failure surfaces as a bridge error, not a silent hang

    return accumulate_transcript(raw_events), state["call_sid"]


@router.websocket("/twilio/stream/{run_id}")
async def twilio_stream(websocket: WebSocket, run_id: int) -> None:
    await websocket.accept()

    run = db.get_run(run_id)
    spec_record = db.get_spec(run.spec_id) if run is not None else None
    lead = db.get_lead(run.lead_id) if run is not None else None
    if run is None or spec_record is None or lead is None:
        await websocket.close()
        return
    spec = AssistantSpec.model_validate_json(spec_record.spec_json)

    try:
        transcript, call_sid = await _bridge_call(websocket, spec, lead)
    except Exception as error:  # a bridge failure must still resolve the run, not strand it
        result = ToolResult(
            tool="reach",
            status="error",
            outcome="provider_error",
            summary=f"The voice bridge failed: {error}",
        )
        db.add_run_step(run_id, "reach", result.model_dump_json())
        tool_exec.write_back_lead_outcome(lead.id, result)
        return

    result = ToolResult(
        tool="reach",
        status="ok",
        outcome="answered",
        summary=f"Called {lead.name} at {lead.phone} — call completed.\n{as_dialogue(transcript)}",
        data={"channel": "voice", "transcript": transcript, "call_sid": call_sid},
    )
    db.add_run_step(run_id, "reach", result.model_dump_json())
    tool_exec.write_back_lead_outcome(lead.id, result)

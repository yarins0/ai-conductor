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
from app.call_prompt import as_dialogue, lead_call_instructions
from app.providers import ToolResult
from app.realtime import REALTIME_MODEL
from app.spec import AssistantSpec

router = APIRouter()  # Twilio-facing webhook/stream paths — no /api prefix

# Injectable seam for tests, same monkeypatch style as anthropic.AsyncAnthropic.
connect_fn = websockets.connect

OPENAI_REALTIME_WS_URL = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
# Not gpt-realtime-whisper: that model requires turn_detection to be null and the
# input buffer committed by hand, which a phone call driven by server VAD never does.
TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
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


def _session_update(spec: AssistantSpec, lead: db.LeadRecord) -> dict[str, Any]:
    # GA shape, and it is unforgiving: `format` must be an object ({"type":
    # "audio/pcmu"}), not the beta's "g711_ulaw" string, and the session needs
    # its own "type": "realtime". Get either wrong and OpenAI rejects the whole
    # session.update with a type error, leaving the session on its pcm16/24kHz
    # defaults — mu-law bytes then read as PCM, so VAD never fires, nothing is
    # transcribed, and the assistant's reply reaches the caller as noise.
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": lead_call_instructions(spec, lead),
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},  # Twilio Media Streams are G.711 mu-law
                    "turn_detection": {"type": "server_vad"},
                    # Pinned so a mis-heard turn can't auto-detect into another
                    # language and flip the conversation (same fix as realtime.py).
                    "transcription": {"model": TRANSCRIPTION_MODEL, "language": spec.language},
                },
                "output": {"format": {"type": "audio/pcmu"}},
            },
        },
    }


def first_error(events: list[dict[str, Any]]) -> str | None:
    """Pure, alongside accumulate_transcript: OpenAI reports a rejected
    session.update as an `error` event on the socket rather than by closing it,
    so without this a misconfigured session is indistinguishable from a call
    where nobody happened to speak."""
    for event in events:
        if event.get("type") == "error":
            error = event.get("error") or {}
            return error.get("message") or json.dumps(error)
    return None


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
) -> tuple[list[dict[str, str]], str | None, str | None]:
    """Relay G.711 mu-law audio between one Twilio Media Stream and one OpenAI
    Realtime session — no conversion, both ends already speak g711_ulaw. Returns
    the accumulated transcript, the Twilio call SID, and the first OpenAI error
    (if any) once the call ends."""
    state: dict[str, Any] = {"stream_sid": None, "call_sid": None}
    raw_events: list[dict[str, Any]] = []

    async with connect_fn(
        OPENAI_REALTIME_WS_URL,
        additional_headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"},
    ) as openai_ws:
        await openai_ws.send(json.dumps(_session_update(spec, lead)))
        # We placed this call, so we speak first. Server VAD only ever responds
        # to the caller, so without this the assistant sits silent waiting for a
        # lead who is waiting for it. Instructions come from the session above.
        await openai_ws.send(json.dumps({"type": "response.create"}))

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

    return accumulate_transcript(raw_events), state["call_sid"], first_error(raw_events)


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
        transcript, call_sid, openai_error = await _bridge_call(websocket, spec, lead)
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

    data = {"channel": "voice", "transcript": transcript, "call_sid": call_sid}
    if transcript:
        # Only a call with words in it is "answered" — that is the claim qualify
        # and the operator both act on, so it has to mean a conversation happened.
        result = ToolResult(
            tool="reach",
            status="ok",
            outcome="answered",
            summary=f"Called {lead.name} at {lead.phone} — call completed.\n{as_dialogue(transcript)}",
            data=data,
        )
    elif openai_error is not None:
        # Twilio connected but OpenAI refused the session: a real failure, not a
        # quiet lead. Reported rather than swallowed — an empty transcript looks
        # identical to voicemail, which is how a rejected session.update hid.
        result = ToolResult(
            tool="reach",
            status="error",
            outcome="provider_error",
            summary=f"The voice bridge failed: {openai_error}",
            data=data,
        )
    else:
        result = ToolResult(
            tool="reach",
            status="ok",
            outcome="no_answer",
            summary=(
                f"Called {lead.name} at {lead.phone} — the call connected but nobody "
                f"spoke (voicemail or dead air). There is no conversation to judge."
            ),
            data=data,
        )
    db.add_run_step(run_id, "reach", result.model_dump_json())
    tool_exec.write_back_lead_outcome(lead.id, result)

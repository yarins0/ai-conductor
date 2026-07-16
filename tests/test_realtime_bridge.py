"""Tests for the Twilio Media Streams <-> OpenAI Realtime bridge (Surface B).

No real network: the OpenAI socket is a fake plugged in via realtime_bridge's
connect_fn seam, Twilio's Media Stream socket is a fake with the same
receive_json/send_json shape as FastAPI's WebSocket, and Twilio's REST call is
mocked the same way tests/test_realtime.py mocks OpenAI's (httpx.AsyncClient.post).
"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

from app import db, realtime_bridge
from app.providers import get_provider
from app.spec import AssistantSpec, ToolConfig

SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Call leads, qualify budget, and book a demo.",
    persona="Friendly and direct.",
    instructions=["Always confirm the lead's name."],
    tools=[ToolConfig(name="reach")],
)


def _bridge_client() -> TestClient:
    # realtime_bridge.router isn't mounted on app.main yet (wired by a parallel
    # task) — a standalone app is enough to exercise its HTTP routes in isolation.
    app = FastAPI()
    app.include_router(realtime_bridge.router)
    return TestClient(app)


# --- twiml_for_run ------------------------------------------------------------


def test_twiml_for_run_strips_https_scheme(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://myapp.ngrok.io")

    xml = realtime_bridge.twiml_for_run(42)

    assert "<Response><Connect><Stream" in xml
    assert '<Stream url="wss://myapp.ngrok.io/twilio/stream/42"/>' in xml


def test_twiml_for_run_with_bare_host(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "myapp.ngrok.io")

    xml = realtime_bridge.twiml_for_run(7)

    assert '<Stream url="wss://myapp.ngrok.io/twilio/stream/7"/>' in xml


# --- /twilio/status/{run_id} --------------------------------------------------


def test_status_callback_no_answer_persists_step_and_marks_lead_unreachable():
    db.init_db()
    lead = db.list_leads()[0]
    spec_record = db.save_spec(SPEC)
    run = db.create_run(spec_record.id, lead.id)
    client = _bridge_client()

    response = client.post(f"/twilio/status/{run.id}", data={"CallStatus": "no-answer"})

    assert response.status_code == 200
    steps = db.list_run_steps(run.id)
    assert len(steps) == 1
    assert json.loads(steps[0].result_json)["outcome"] == "no_answer"
    assert db.get_lead(lead.id).status == "unreachable"


def test_status_callback_completed_persists_nothing():
    db.init_db()
    lead = db.list_leads()[1]
    spec_record = db.save_spec(SPEC)
    run = db.create_run(spec_record.id, lead.id)
    client = _bridge_client()

    response = client.post(f"/twilio/status/{run.id}", data={"CallStatus": "completed"})

    assert response.status_code == 200
    assert db.list_run_steps(run.id) == []


# --- accumulate_transcript ----------------------------------------------------


def test_accumulate_transcript_picks_relevant_events_and_ignores_others():
    events = [
        {"type": "session.updated"},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Send me pricing."},
        {"type": "response.output_audio.delta", "delta": "base64chunk"},
        {"type": "response.output_audio_transcript.done", "transcript": "Sure, I can do that."},
        {"type": "input_audio_buffer.speech_started"},
    ]

    transcript = realtime_bridge.accumulate_transcript(events)

    assert transcript == [
        {"speaker": "lead", "text": "Send me pricing."},
        {"speaker": "assistant", "text": "Sure, I can do that."},
    ]


def test_accumulate_transcript_empty_for_no_matching_events():
    assert realtime_bridge.accumulate_transcript([{"type": "session.updated"}]) == []


# --- first_error ---------------------------------------------------------------
#
# The regression these lock down: a rejected session.update produced an `error`
# event that was collected and then dropped, so a misconfigured session looked
# exactly like a call where nobody spoke — a real Twilio call reached voicemail,
# was recorded "answered", and got qualified off an empty transcript.


def test_first_error_returns_openai_error_message():
    events = [
        {"type": "session.created"},
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Invalid type for 'session.audio.input.format': expected an object, but got a string instead.",
            },
        },
    ]

    assert "expected an object" in realtime_bridge.first_error(events)


def test_first_error_is_none_for_a_clean_session():
    assert realtime_bridge.first_error([{"type": "session.updated"}]) is None


# --- _bridge_call relay --------------------------------------------------------


class _FakeTwilioSocket:
    """Same receive_json/send_json shape as FastAPI's WebSocket."""

    def __init__(self, frames: list[dict]) -> None:
        self._frames = list(frames)
        self.sent: list[dict] = []

    async def receive_json(self) -> dict:
        await asyncio.sleep(0)  # cooperative yield, so both relay loops interleave deterministically
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)

    async def send_json(self, data: dict) -> None:
        await asyncio.sleep(0)
        self.sent.append(data)


class _FakeOpenAISocket:
    """Same shape as a websockets.connect() connection: async context manager,
    async iterator of incoming frames, and .send() for outgoing ones."""

    def __init__(self, incoming: list[dict]) -> None:
        self._incoming = list(incoming)
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        await asyncio.sleep(0)
        self.sent.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)
        if not self._incoming:
            raise StopAsyncIteration
        return json.dumps(self._incoming.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


def test_bridge_relays_media_unmodified_and_sends_session_update_first(monkeypatch):
    lead = db.create_lead("Voice Lead", "Voice Co", "+15550001111", notes="Prefers mornings")
    twilio_socket = _FakeTwilioSocket([
        {"event": "start", "start": {"streamSid": "MZ123", "callSid": "CA123"}},
        {"event": "media", "media": {"payload": "twilio-audio-chunk"}},
        {"event": "stop"},
    ])
    openai_socket = _FakeOpenAISocket([
        {"type": "response.output_audio.delta", "delta": "openai-audio-chunk"},
    ])
    monkeypatch.setattr(realtime_bridge, "connect_fn", lambda url, **kwargs: openai_socket)

    transcript, call_sid, openai_error = asyncio.run(
        realtime_bridge._bridge_call(twilio_socket, SPEC, lead)
    )

    assert call_sid == "CA123"
    assert transcript == []  # no transcript events in this canned exchange
    assert openai_error is None

    # session.update sent before anything else, mu-law both ways, notes included.
    # The GA shapes are asserted literally because getting them wrong is not a
    # crash: OpenAI rejects the session.update and the call silently goes mute.
    assert openai_socket.sent[0]["type"] == "session.update"
    session = openai_socket.sent[0]["session"]
    assert session["type"] == "realtime"
    assert session["audio"]["input"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["output"]["format"] == {"type": "audio/pcmu"}
    assert session["audio"]["input"]["transcription"]["language"] == "en"
    assert "Prefers mornings" in session["instructions"]

    # ...and we greet first: an outbound call whose assistant waits for the lead
    # to speak is a call the lead hears as silence.
    assert openai_socket.sent[1] == {"type": "response.create"}

    # Twilio -> OpenAI: media payload forwarded verbatim, no conversion
    forwarded = [m for m in openai_socket.sent if m["type"] == "input_audio_buffer.append"]
    assert forwarded == [{"type": "input_audio_buffer.append", "audio": "twilio-audio-chunk"}]

    # OpenAI -> Twilio: audio delta forwarded verbatim, wrapped in Twilio's media event
    media_to_twilio = [f for f in twilio_socket.sent if f["event"] == "media"]
    assert media_to_twilio == [
        {"event": "media", "streamSid": "MZ123", "media": {"payload": "openai-audio-chunk"}}
    ]

    db.delete_lead(lead.id)  # shared test DB — other modules assert an exact seeded-lead count


def test_bridge_clears_twilio_audio_on_speech_started_barge_in(monkeypatch):
    lead = db.create_lead("Barge Lead", "Barge Co", "+15550002222")
    twilio_socket = _FakeTwilioSocket([
        {"event": "start", "start": {"streamSid": "MZ456", "callSid": "CA456"}},
        {"event": "stop"},
    ])
    openai_socket = _FakeOpenAISocket([
        {"type": "input_audio_buffer.speech_started"},
    ])
    monkeypatch.setattr(realtime_bridge, "connect_fn", lambda url, **kwargs: openai_socket)

    asyncio.run(realtime_bridge._bridge_call(twilio_socket, SPEC, lead))

    clear_events = [f for f in twilio_socket.sent if f["event"] == "clear"]
    assert clear_events == [{"event": "clear", "streamSid": "MZ456"}]

    db.delete_lead(lead.id)


# --- TwilioReachProvider URL construction -------------------------------------


def _twilio_env(monkeypatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC1")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")


def test_twilio_reach_uses_bridge_urls_when_run_id_and_public_base_url_present(monkeypatch):
    _twilio_env(monkeypatch)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://myapp.ngrok.io")
    calls = []

    async def _post(self, url, **kwargs):
        calls.append((url, kwargs.get("data")))
        return httpx.Response(200, json={"sid": "CA999", "status": "queued"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    lead = db.create_lead("Bridge Lead", "Bridge Co", "+15551234567")

    result = asyncio.run(get_provider("reach", "twilio").execute(lead, {"run_id": 55}))

    assert result.outcome == "initiated"
    (_, data), = calls
    assert data["Url"] == "https://myapp.ngrok.io/twilio/voice/55"
    assert data["StatusCallback"] == "https://myapp.ngrok.io/twilio/status/55"
    assert data["StatusCallbackEvent"] == "completed no-answer busy failed"

    db.delete_lead(lead.id)  # shared test DB — other modules assert an exact seeded-lead count


def test_twilio_reach_falls_back_to_demo_url_without_run_id_or_public_base_url(monkeypatch):
    _twilio_env(monkeypatch)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    calls = []

    async def _post(self, url, **kwargs):
        calls.append((url, kwargs.get("data")))
        return httpx.Response(200, json={"sid": "CA000", "status": "queued"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    lead = db.create_lead("No Bridge Lead", "No Bridge Co", "+15551234568")

    result = asyncio.run(get_provider("reach", "twilio").execute(lead, {}))

    assert result.outcome == "initiated"
    (_, data), = calls
    assert data["Url"] == "http://demo.twilio.com/docs/voice.xml"
    assert "StatusCallback" not in data

    db.delete_lead(lead.id)

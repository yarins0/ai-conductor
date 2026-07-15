"""OpenAI Realtime control plane for the operator session (Surface A).

The browser talks to OpenAI directly over WebRTC — audio never touches this
server. What the server owns is everything that must not live in a browser:
minting the ephemeral session token (the real API key stays here), executing
tool calls against the provider layer, and bridging web_search to OpenAI's
Responses API (the Realtime API has function calling but not the built-in
web_search tool).

No OpenAI SDK — raw REST via httpx, matching the Twilio/HubSpot precedent.
All OpenAI HTTP goes through `_openai_post` so a field-name drift in their
API is a one-place fix.
"""

import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app import db, tool_exec
from app.realtime_tools import operator_instructions, realtime_tools_for_spec
from app.spec import AssistantSpec

router = APIRouter(prefix="/api/realtime")

REALTIME_MODEL = "gpt-realtime"
# The Responses API model used to fulfill bridged web_search calls.
WEB_SEARCH_MODEL = "gpt-5"
OPENAI_BASE_URL = "https://api.openai.com/v1"
# Web search + token minting are interactive but not instant; generous enough
# to never cut off a slow search, short enough to fail a wedged request.
OPENAI_TIMEOUT_SECONDS = 60.0


class TokenRequest(BaseModel):
    spec_id: int


class ToolCallRequest(BaseModel):
    spec_id: int
    lead_id: int
    provider_id: str | None = None
    args: dict[str, Any] = {}


class WebSearchRequest(BaseModel):
    query: str


async def _openai_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the OpenAI REST API; any failure becomes a 502 with a readable
    message (the operator's UI shows it), never a raw traceback."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=502, detail="OPENAI_API_KEY is not configured.")
    async with httpx.AsyncClient(timeout=OPENAI_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{OPENAI_BASE_URL}{path}",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI request failed ({response.status_code}): {response.text[:300]}",
        )
    return response.json()


def _load_spec(spec_id: int) -> AssistantSpec:
    record = db.get_spec(spec_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No spec with id {spec_id}.")
    return AssistantSpec.model_validate_json(record.spec_json)


@router.post("/token")
async def mint_token(request: TokenRequest) -> dict[str, Any]:
    """Mint an ephemeral client secret for one operator session, configured with
    the spec's instructions and exactly its allowed tools. The browser uses it
    to open the WebRTC session; it expires on its own, so nothing to revoke."""
    spec = _load_spec(request.spec_id)
    payload = {
        "session": {
            "type": "realtime",
            "model": REALTIME_MODEL,
            "instructions": operator_instructions(spec),
            "tools": realtime_tools_for_spec(spec),
            # Same nested audio shape as realtime_bridge._session_update, minus the
            # g711_ulaw format (WebRTC negotiates its own codec) — gives the UI the
            # operator's own words via input transcription, and a fixed voice.
            "audio": {
                "input": {"transcription": {"model": "gpt-realtime-whisper"}},
                "output": {"voice": "marin"},
            },
        }
    }
    data = await _openai_post("/realtime/client_secrets", payload)
    return {"value": data.get("value"), "model": REALTIME_MODEL}


@router.post("/tools/{tool_name}")
async def execute_tool(tool_name: str, request: ToolCallRequest) -> dict[str, Any]:
    """Execute one tool call from the operator's Realtime session. Stateless:
    the run row is found or created per (spec, lead), so a conversation's
    actions accumulate in the Company Brain without a server-side session."""
    spec = _load_spec(request.spec_id)
    lead = db.get_lead(request.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {request.lead_id}.")

    # Explicit selection wins; otherwise the persisted per-tool setting; the
    # registry default resolves inside run_tool when both are absent.
    provider_id = request.provider_id or db.get_provider_settings().get(tool_name)

    try:
        run_id, result = await tool_exec.run_tool(
            request.spec_id, spec, lead, tool_name, provider_id, request.args
        )
    except tool_exec.ToolNotAllowed as error:
        raise HTTPException(status_code=400, detail=str(error))

    return {"run_id": run_id, "result": result.model_dump()}


@router.post("/web-search")
async def web_search(request: WebSearchRequest) -> dict[str, str]:
    """Bridge the Realtime session's web_search function to the Responses API's
    built-in web_search tool."""
    data = await _openai_post(
        "/responses",
        {
            "model": WEB_SEARCH_MODEL,
            "input": request.query,
            "tools": [{"type": "web_search"}],
        },
    )
    return {"result": _extract_output_text(data)}


def _extract_output_text(response: dict[str, Any]) -> str:
    """Pull the assistant's text out of a raw Responses API payload (the SDK's
    `output_text` convenience, reimplemented for REST)."""
    parts = [
        content.get("text", "")
        for item in response.get("output", [])
        if item.get("type") == "message"
        for content in item.get("content", [])
        if content.get("type") == "output_text"
    ]
    return "\n".join(part for part in parts if part)

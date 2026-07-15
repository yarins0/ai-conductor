"""FastAPI service: Builder API + static frontend.

Run with: python -m app.main
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# SSE tuning: poll cadence for new run steps and a safety cap so a run that
# never reaches a terminal status can't stream forever (runs always call
# finish_run, so the cap is a guard, not the normal exit).
STREAM_POLL_SECONDS = 0.3
MAX_STREAM_POLLS = 200
TERMINAL_RUN_STATUSES = ("completed", "failed")

# Load ANTHROPIC_API_KEY (and optional AI_CONDUCTOR_DB) before app modules
# read the environment at import time.
load_dotenv("secrets/.env")

from app import builder, db, live_agent, runtime  # noqa: E402
from app.spec import AssistantSpec  # noqa: E402
from app.providers import ProviderConfigError, get_provider, list_providers  # noqa: E402

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AI Conductor", lifespan=lifespan)

# Strong references to in-flight run tasks (see create_run).
_background_runs: set[asyncio.Task] = set()


class GenerateSpecRequest(BaseModel):
    description: str


class RunRequest(BaseModel):
    spec_id: int
    lead_id: int
    providers: dict[str, str] | None = None  # per-run tool -> provider_id selection


class EditSpecRequest(BaseModel):
    instruction: str


class CreateLeadRequest(BaseModel):
    name: str
    company: str
    phone: str
    sim_profile: str | None = None
    email: str | None = None
    notes: str | None = None


class UpdateLeadRequest(BaseModel):
    name: str | None = None
    company: str | None = None
    phone: str | None = None
    email: str | None = None
    notes: str | None = None
    sim_profile: str | None = None


def spec_record_to_response(record: db.SpecRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "spec": json.loads(record.spec_json),
    }


def run_record_to_response(record: db.RunRecord) -> dict:
    return {
        "id": record.id,
        "spec_id": record.spec_id,
        "lead_id": record.lead_id,
        "status": record.status,
        "created_at": record.created_at.isoformat(),
    }


def lead_record_to_response(record: db.LeadRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "company": record.company,
        "phone": record.phone,
        "status": record.status,
        "intent_score": record.intent_score,
        "booked_slot": record.booked_slot,
        "sim_profile": record.sim_profile,
        "email": record.email,
        "notes": record.notes,
    }


@app.post("/api/specs/generate", status_code=201)
def generate_spec(request: GenerateSpecRequest) -> dict:
    try:
        spec: AssistantSpec = builder.create_spec(request.description)
    except builder.BuilderError as builder_error:
        # Invalid LLM output fails cleanly and is never persisted (rule #2).
        raise HTTPException(status_code=422, detail=str(builder_error))
    except anthropic.APIError as api_error:
        raise HTTPException(
            status_code=502, detail=f"The Builder's LLM call failed: {api_error.message}"
        )
    record = db.save_spec(spec)
    return spec_record_to_response(record)


@app.get("/api/specs")
def list_specs() -> list[dict]:
    return [spec_record_to_response(record) for record in db.list_specs()]


@app.get("/api/specs/{spec_id}")
def get_spec(spec_id: int) -> dict:
    record = db.get_spec(spec_id)
    if record is None:
        # Explicit not-found, never a 500 (rule #6).
        raise HTTPException(status_code=404, detail=f"No spec with id {spec_id}.")
    return spec_record_to_response(record)


@app.post("/api/specs/{spec_id}/edit")
def edit_spec(spec_id: int, request: EditSpecRequest) -> dict:
    spec_record = db.get_spec(spec_id)
    if spec_record is None:
        raise HTTPException(status_code=404, detail=f"No spec with id {spec_id}.")
    current = AssistantSpec.model_validate_json(spec_record.spec_json)
    try:
        # The edit loop self-corrects on bad tool args and falls back to whole-spec
        # regeneration internally, so it returns a valid spec or raises cleanly.
        edited = builder.edit_spec(current, request.instruction)
    except builder.BuilderError as builder_error:
        raise HTTPException(status_code=422, detail=str(builder_error))
    except anthropic.APIError as api_error:
        raise HTTPException(
            status_code=502, detail=f"The Builder's LLM call failed: {api_error.message}"
        )
    updated = db.update_spec(spec_id, edited)
    return spec_record_to_response(updated)


def _provider_preflight(spec: AssistantSpec, selection: dict[str, str]) -> str | None:
    """Check every tool resolves to a provider whose credentials are present.
    Returns the first problem as a user-facing message, or None if all clear.
    Shared by the scripted-run and live-session paths so both reject a bad
    selection the same way, before any run row exists."""
    for tool in spec.tools:
        provider = get_provider(tool.name, selection.get(tool.name))
        if provider is None:
            return f"No provider '{selection.get(tool.name)}' for tool '{tool.name}'."
        try:
            provider.check_credentials()
        except ProviderConfigError as error:
            return str(error)
    return None


@app.post("/api/runs", status_code=202)
async def create_run(request: RunRequest) -> dict:
    spec_record = db.get_spec(request.spec_id)
    if spec_record is None:
        raise HTTPException(status_code=404, detail=f"No spec with id {request.spec_id}.")
    lead = db.get_lead(request.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {request.lead_id}.")

    spec = AssistantSpec.model_validate_json(spec_record.spec_json)

    # Persisted per-tool settings are the baseline; an explicit per-request
    # selection still wins. An empty settings table makes this identical to
    # request.providers alone, so today's behavior is unchanged.
    providers = {**db.get_provider_settings(), **(request.providers or {})}

    # Credential preflight: reject an unknown provider or a real one missing its
    # env vars BEFORE any run row is created, so a bad selection never strands a run.
    preflight_error = _provider_preflight(spec, providers)
    if preflight_error is not None:
        raise HTTPException(status_code=400, detail=preflight_error)

    run = db.create_run(spec_id=request.spec_id, lead_id=request.lead_id)
    # Fire-and-forget: the run drives itself via the Runtime and persists each
    # step as it goes, so the caller doesn't block on the full reach/qualify/book
    # sequence. The task set keeps a strong reference — the event loop only holds
    # weak refs, so an unreferenced task can be garbage-collected mid-run.
    task = asyncio.create_task(runtime.execute_run(run.id, spec, lead, providers))
    _background_runs.add(task)
    task.add_done_callback(_background_runs.discard)
    return run_record_to_response(run)


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return [run_record_to_response(record) for record in db.list_runs()]


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    record = db.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No run with id {run_id}.")
    steps = [
        {"tool": step.tool, "result": json.loads(step.result_json), "created_at": step.created_at.isoformat()}
        for step in db.list_run_steps(run_id)
    ]
    return {"run": run_record_to_response(record), "steps": steps}


def _step_event(step: db.RunStepRecord) -> str:
    payload = {
        "type": "step",
        "tool": step.tool,
        "result": json.loads(step.result_json),
        "created_at": step.created_at.isoformat(),
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _run_step_stream(run_id: int) -> AsyncIterator[str]:
    # Every connection replays all persisted steps first (emitted starts at 0),
    # so a reconnect rebuilds the whole view from durable rows — the stream is a
    # push layer over durable state, never the source of truth (rule #5).
    emitted = 0
    for _ in range(MAX_STREAM_POLLS):
        steps = db.list_run_steps(run_id)
        for step in steps[emitted:]:
            yield _step_event(step)
        emitted = len(steps)

        # ponytail: sync db reads inside the async generator briefly touch the
        # event loop each poll — fine at single-user scale; no async db needed.
        run = db.get_run(run_id)
        if run is not None and run.status in TERMINAL_RUN_STATUSES:
            yield f"data: {json.dumps({'type': 'done', 'status': run.status})}\n\n"
            return

        await asyncio.sleep(STREAM_POLL_SECONDS)

    final = db.get_run(run_id)
    status = final.status if final is not None else "failed"
    yield f"data: {json.dumps({'type': 'done', 'status': status})}\n\n"


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: int) -> StreamingResponse:
    # 404 before streaming starts, so a bad id is a clean error not a dead stream.
    if db.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail=f"No run with id {run_id}.")
    return StreamingResponse(
        _run_step_stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.websocket("/api/live/{spec_id}")
async def live_session(websocket: WebSocket, spec_id: int) -> None:
    """Live voice/agentic mode (Phase 6). The browser handles speech I/O and sends
    each user turn as text; the model decides which registered tool to invoke,
    non-linearly, and its actions persist to the Company Brain exactly like a
    scripted run. A sibling to create_run/stream_run — the linear Runtime is
    untouched.

    Protocol: client sends {"type":"start","lead_id","providers"} once, then
    {"type":"user","text"} per turn; server replies with a {"type":"ready","run_id"},
    then per turn the live_agent events ({"type":"assistant"|"action"|"error"})
    followed by a {"type":"turn_done"} marking the turn complete."""
    await websocket.accept()

    spec_record = db.get_spec(spec_id)
    if spec_record is None:
        await websocket.send_json({"type": "error", "message": f"No spec with id {spec_id}."})
        await websocket.close()
        return
    spec = AssistantSpec.model_validate_json(spec_record.spec_json)

    try:
        start = await websocket.receive_json()
    except WebSocketDisconnect:
        return

    lead = db.get_lead(start.get("lead_id"))
    if lead is None:
        await websocket.send_json({"type": "error", "message": "Lead not found."})
        await websocket.close()
        return

    selection = start.get("providers") or {}
    preflight_error = _provider_preflight(spec, selection)
    if preflight_error is not None:
        await websocket.send_json({"type": "error", "message": preflight_error})
        await websocket.close()
        return

    run = db.create_run(spec_id=spec_id, lead_id=lead.id)
    session = live_agent.LiveSession(spec, lead, run.id, selection)
    await websocket.send_json({"type": "ready", "run_id": run.id})

    # Each user turn is driven to completion (the model may chain several tool
    # calls) before the next is read. finish_run on disconnect so the run row is
    # terminal and the conversation's steps are inspectable in the Company Brain.
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") != "user":
                continue
            user_text = (message.get("text") or "").strip()
            if not user_text:
                continue
            async for event in session.handle_turn(user_text):
                await websocket.send_json(event)
            # Explicit turn boundary: lets the browser resume listening only once a
            # full turn (which may chain several tool calls) is done, so the mic
            # never reopens between the assistant's own spoken fragments.
            await websocket.send_json({"type": "turn_done"})
    except WebSocketDisconnect:
        db.finish_run(run.id, "completed")


@app.get("/api/providers")
def get_providers() -> dict:
    return list_providers()


@app.get("/api/leads")
def list_leads(q: str | None = None) -> list[dict]:
    leads = db.list_leads()
    if q:
        needle = q.lower()
        # ponytail: in-memory filter, move to SQL if leads ever number in the thousands
        leads = [lead for lead in leads if needle in lead.name.lower() or needle in lead.company.lower()]
    return [lead_record_to_response(record) for record in leads]


@app.post("/api/leads", status_code=201)
def create_lead(request: CreateLeadRequest) -> dict:
    record = db.create_lead(
        request.name,
        request.company,
        request.phone,
        request.sim_profile,
        email=request.email,
        notes=request.notes,
    )
    return lead_record_to_response(record)


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int) -> dict:
    record = db.get_lead(lead_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}.")
    return lead_record_to_response(record)


@app.patch("/api/leads/{lead_id}")
def update_lead(lead_id: int, request: UpdateLeadRequest) -> dict:
    fields = request.model_dump(exclude_unset=True)
    record = db.update_lead(lead_id, **fields)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}.")
    return lead_record_to_response(record)


@app.delete("/api/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: int) -> None:
    if not db.delete_lead(lead_id):
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}.")


@app.get("/api/settings")
def get_settings() -> dict[str, str]:
    persisted = db.get_provider_settings()
    return {
        tool: persisted.get(tool) or next(p["id"] for p in providers if p["default"])
        for tool, providers in list_providers().items()
    }


@app.put("/api/settings")
def set_settings(mapping: dict[str, str]) -> dict[str, str]:
    for tool, provider_id in mapping.items():
        if get_provider(tool, provider_id) is None:
            # Unknown tool or unknown provider id for that tool — 400, nothing persisted.
            raise HTTPException(status_code=400, detail=f"No provider '{provider_id}' for tool '{tool}'.")
    db.set_provider_settings(mapping)
    return get_settings()


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    # 8123 default: port 8000 hits WinError 10013 (reserved/blocked) on some
    # Windows setups. Override with the PORT env var if 8123 is also taken.
    uvicorn.run("app.main:app", port=int(os.getenv("PORT", "8123")), reload=True)

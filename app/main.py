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
from fastapi import FastAPI, HTTPException
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
load_dotenv()

from app import builder, db, runtime  # noqa: E402
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


@app.post("/api/runs", status_code=202)
async def create_run(request: RunRequest) -> dict:
    spec_record = db.get_spec(request.spec_id)
    if spec_record is None:
        raise HTTPException(status_code=404, detail=f"No spec with id {request.spec_id}.")
    lead = db.get_lead(request.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {request.lead_id}.")

    spec = AssistantSpec.model_validate_json(spec_record.spec_json)

    # Credential preflight: reject an unknown provider or a real one missing its
    # env vars BEFORE any run row is created, so a bad selection never strands a run.
    selection = request.providers or {}
    for tool in spec.tools:
        provider = get_provider(tool.name, selection.get(tool.name))
        if provider is None:
            raise HTTPException(status_code=400, detail=f"No provider '{selection.get(tool.name)}' for tool '{tool.name}'.")
        try:
            provider.check_credentials()
        except ProviderConfigError as error:
            raise HTTPException(status_code=400, detail=str(error))

    run = db.create_run(spec_id=request.spec_id, lead_id=request.lead_id)
    # Fire-and-forget: the run drives itself via the Runtime and persists each
    # step as it goes, so the caller doesn't block on the full reach/qualify/book
    # sequence. The task set keeps a strong reference — the event loop only holds
    # weak refs, so an unreferenced task can be garbage-collected mid-run.
    task = asyncio.create_task(runtime.execute_run(run.id, spec, lead, request.providers))
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


@app.get("/api/providers")
def get_providers() -> dict:
    return list_providers()


@app.get("/api/leads")
def list_leads() -> list[dict]:
    return [lead_record_to_response(record) for record in db.list_leads()]


@app.post("/api/leads", status_code=201)
def create_lead(request: CreateLeadRequest) -> dict:
    record = db.create_lead(request.name, request.company, request.phone, request.sim_profile)
    return lead_record_to_response(record)


@app.get("/api/leads/{lead_id}")
def get_lead(lead_id: int) -> dict:
    record = db.get_lead(lead_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {lead_id}.")
    return lead_record_to_response(record)


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    # 8123 default: port 8000 hits WinError 10013 (reserved/blocked) on some
    # Windows setups. Override with the PORT env var if 8123 is also taken.
    uvicorn.run("app.main:app", port=int(os.getenv("PORT", "8123")), reload=True)

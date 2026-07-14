"""FastAPI service: Builder API + static frontend.

Run with: python -m app.main
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load ANTHROPIC_API_KEY (and optional AI_CONDUCTOR_DB) before app modules
# read the environment at import time.
load_dotenv()

from app import builder, db, runtime  # noqa: E402
from app.spec import AssistantSpec  # noqa: E402

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


def spec_record_to_response(record: db.SpecRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "created_at": record.created_at.isoformat(),
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


@app.post("/api/runs", status_code=202)
async def create_run(request: RunRequest) -> dict:
    spec_record = db.get_spec(request.spec_id)
    if spec_record is None:
        raise HTTPException(status_code=404, detail=f"No spec with id {request.spec_id}.")
    lead = db.get_lead(request.lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail=f"No lead with id {request.lead_id}.")

    spec = AssistantSpec.model_validate_json(spec_record.spec_json)
    run = db.create_run(spec_id=request.spec_id, lead_id=request.lead_id)
    # Fire-and-forget: the run drives itself via the Runtime and persists each
    # step as it goes, so the caller doesn't block on the full reach/qualify/book
    # sequence. The task set keeps a strong reference — the event loop only holds
    # weak refs, so an unreferenced task can be garbage-collected mid-run.
    task = asyncio.create_task(runtime.execute_run(run.id, spec, lead))
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


@app.get("/api/leads")
def list_leads() -> list[dict]:
    return [lead_record_to_response(record) for record in db.list_leads()]


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

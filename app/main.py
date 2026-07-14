"""FastAPI service: Builder API + static frontend.

Run with: python -m app.main
"""

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

from app import builder, db  # noqa: E402
from app.spec import AssistantSpec  # noqa: E402

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AI Conductor", lifespan=lifespan)


class GenerateSpecRequest(BaseModel):
    description: str


def spec_record_to_response(record: db.SpecRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "created_at": record.created_at.isoformat(),
        "spec": json.loads(record.spec_json),
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


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    # 8123 default: port 8000 hits WinError 10013 (reserved/blocked) on some
    # Windows setups. Override with the PORT env var if 8123 is also taken.
    uvicorn.run("app.main:app", port=int(os.getenv("PORT", "8123")), reload=True)

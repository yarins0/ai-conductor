"""API endpoint tests with the LLM builder mocked out — no network calls."""

import pytest
from fastapi.testclient import TestClient

from app import builder, db, main
from app.spec import AssistantSpec, ToolConfig

SAMPLE_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Call leads, qualify budget, and book a demo.",
    persona="Friendly and direct.",
    instructions=["Always confirm the lead's name."],
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)


@pytest.fixture()
def client() -> TestClient:
    db.init_db()
    return TestClient(main.app)


def test_generate_spec_persists_and_returns_spec(client, monkeypatch):
    monkeypatch.setattr(builder, "create_spec", lambda description: SAMPLE_SPEC)
    response = client.post("/api/specs/generate", json={"description": "an SDR bot"})
    assert response.status_code == 201
    body = response.json()
    assert body["spec"]["name"] == "SDR Assistant"

    fetched = client.get(f"/api/specs/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["spec"] == body["spec"]


def test_generate_spec_invalid_llm_output_returns_422(client, monkeypatch):
    def raise_builder_error(description):
        raise builder.BuilderError("The Builder produced an invalid spec.")

    monkeypatch.setattr(builder, "create_spec", raise_builder_error)
    response = client.post("/api/specs/generate", json={"description": "an SDR bot"})
    assert response.status_code == 422
    assert "invalid spec" in response.json()["detail"]


def test_get_missing_spec_returns_404(client):
    response = client.get("/api/specs/999999")
    assert response.status_code == 404


async def _noop_execute_run(run_id, spec, lead):
    pass


def _first_lead_id() -> int:
    return db.list_leads()[0].id


def test_create_run_returns_202_and_run_record(client, monkeypatch):
    monkeypatch.setattr(main.runtime, "execute_run", _noop_execute_run)
    spec_record = db.save_spec(SAMPLE_SPEC)
    lead_id = _first_lead_id()

    response = client.post("/api/runs", json={"spec_id": spec_record.id, "lead_id": lead_id})

    assert response.status_code == 202
    body = response.json()
    assert body["spec_id"] == spec_record.id
    assert body["lead_id"] == lead_id
    assert body["status"] == "running"


def test_create_run_missing_spec_returns_404(client, monkeypatch):
    monkeypatch.setattr(main.runtime, "execute_run", _noop_execute_run)
    lead_id = _first_lead_id()

    response = client.post("/api/runs", json={"spec_id": 999999, "lead_id": lead_id})

    assert response.status_code == 404


def test_create_run_missing_lead_returns_404(client, monkeypatch):
    monkeypatch.setattr(main.runtime, "execute_run", _noop_execute_run)
    spec_record = db.save_spec(SAMPLE_SPEC)

    response = client.post("/api/runs", json={"spec_id": spec_record.id, "lead_id": 999999})

    assert response.status_code == 404


def test_get_missing_run_returns_404(client):
    response = client.get("/api/runs/999999")
    assert response.status_code == 404


def test_get_run_returns_run_and_steps(client):
    spec_record = db.save_spec(SAMPLE_SPEC)
    lead_id = _first_lead_id()
    run = db.create_run(spec_id=spec_record.id, lead_id=lead_id)
    db.add_run_step(run.id, "reach", '{"tool": "reach", "status": "ok", "outcome": "answered", "summary": "ok", "data": {}}')

    response = client.get(f"/api/runs/{run.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == run.id
    assert len(body["steps"]) == 1
    assert body["steps"][0]["tool"] == "reach"


def test_list_leads_returns_seeded_leads(client):
    response = client.get("/api/leads")
    assert response.status_code == 200
    assert len(response.json()) == 3


def test_get_missing_lead_returns_404(client):
    response = client.get("/api/leads/999999")
    assert response.status_code == 404

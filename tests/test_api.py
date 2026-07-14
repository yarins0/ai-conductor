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

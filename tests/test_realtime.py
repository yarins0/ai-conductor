"""Realtime control-plane endpoint tests — OpenAI mocked out, no network."""

import pytest
from fastapi.testclient import TestClient

import app.providers as providers
from app import db, main
from app.spec import AssistantSpec, ToolConfig

FULL_SPEC = AssistantSpec(
    name="SDR Assistant",
    objective="Call leads, qualify budget, and book a demo.",
    persona="Friendly and direct.",
    instructions=["Always confirm the lead's name."],
    tools=[ToolConfig(name="reach"), ToolConfig(name="qualify"), ToolConfig(name="book")],
)

BOOK_ONLY_SPEC = AssistantSpec(
    name="Booker",
    objective="Book meetings for the operator.",
    persona="Efficient.",
    instructions=[],
    tools=[ToolConfig(name="book")],
)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    db.init_db()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(providers, "STEP_DELAY_SECONDS", 0)
    return TestClient(main.app)


def _first_lead_id() -> int:
    return db.list_leads()[0].id


# --- /api/realtime/token -----------------------------------------------------


def test_token_scopes_tools_to_spec(client, fake_openai_http):
    calls = fake_openai_http({"client_secrets": {"value": "ek_test"}})
    record = db.save_spec(BOOK_ONLY_SPEC)

    response = client.post("/api/realtime/token", json={"spec_id": record.id})

    assert response.status_code == 200
    assert response.json() == {
        "value": "ek_test",
        "model": "gpt-realtime",
        # The version the frozen tools below came from — the browser polls
        # against this to notice the spec being edited out from under it.
        "spec_version": record.updated_at.isoformat(),
    }
    (_, payload), = calls
    tool_names = [tool["name"] for tool in payload["session"]["tools"]]
    # Only the spec's tools plus the three universal conversation functions —
    # a book-only assistant must not be offered reach or qualify.
    assert tool_names == ["book", "list_leads", "request_lead", "web_search"]
    assert "Booker" in payload["session"]["instructions"]


def test_token_spec_version_tracks_edits_and_matches_the_polled_field(client, fake_openai_http):
    """The spec-drift notice rests entirely on these two agreeing: the token's
    spec_version is the baseline, and GET /api/specs/{id}.updated_at is what the
    browser polls against it. If an edit failed to move that field, or the two
    endpoints serialized it differently, the session would never notice it had
    gone stale — and the failure would be silent, which is the bug being fixed."""
    fake_openai_http({"client_secrets": {"value": "ek_test"}})
    record = db.save_spec(BOOK_ONLY_SPEC)
    minted = client.post("/api/realtime/token", json={"spec_id": record.id})
    before = minted.json()["spec_version"]

    db.update_spec(record.id, BOOK_ONLY_SPEC)  # content is irrelevant; the edit is the event

    after = client.post("/api/realtime/token", json={"spec_id": record.id}).json()["spec_version"]
    assert after != before
    assert client.get(f"/api/specs/{record.id}").json()["updated_at"] == after


def test_token_missing_spec_returns_404(client, fake_openai_http):
    fake_openai_http({})
    response = client.post("/api/realtime/token", json={"spec_id": 999999})
    assert response.status_code == 404


def test_token_missing_api_key_returns_502(client, monkeypatch, fake_openai_http):
    fake_openai_http({})
    monkeypatch.delenv("OPENAI_API_KEY")
    record = db.save_spec(BOOK_ONLY_SPEC)

    response = client.post("/api/realtime/token", json={"spec_id": record.id})

    assert response.status_code == 502
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_token_openai_error_returns_502(client, monkeypatch, fake_openai_http):
    import httpx

    async def _post(self, url, **kwargs):
        return httpx.Response(401, text="bad key")

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    record = db.save_spec(BOOK_ONLY_SPEC)

    response = client.post("/api/realtime/token", json={"spec_id": record.id})

    assert response.status_code == 502


# --- /api/realtime/tools/{tool_name} -----------------------------------------


def test_execute_tool_persists_and_reuses_run(client):
    record = db.save_spec(BOOK_ONLY_SPEC)
    lead_id = _first_lead_id()

    first = client.post(
        "/api/realtime/tools/book",
        json={"spec_id": record.id, "lead_id": lead_id, "args": {}},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["result"]["outcome"] == "booked"

    # Same (spec, lead) pair keeps accumulating into the same run row.
    second = client.post(
        "/api/realtime/tools/book",
        json={"spec_id": record.id, "lead_id": lead_id, "args": {}},
    )
    assert second.json()["run_id"] == body["run_id"]

    steps = db.list_run_steps(body["run_id"])
    assert [step.tool for step in steps] == ["book", "book"]

    lead = db.get_lead(lead_id)
    assert lead.status == "booked"


def test_execute_tool_not_in_spec_returns_400(client):
    record = db.save_spec(BOOK_ONLY_SPEC)
    response = client.post(
        "/api/realtime/tools/reach",
        json={"spec_id": record.id, "lead_id": _first_lead_id(), "args": {}},
    )
    assert response.status_code == 400


def test_execute_tool_missing_lead_returns_404(client):
    record = db.save_spec(BOOK_ONLY_SPEC)
    response = client.post(
        "/api/realtime/tools/book",
        json={"spec_id": record.id, "lead_id": 999999, "args": {}},
    )
    assert response.status_code == 404


def test_execute_tool_uses_persisted_provider_setting(client, monkeypatch):
    # Persisted setting picks the credential-gated Google provider with no env
    # vars set: the call must come back as a spoken config error, not a 500.
    # Explicitly unset — app.main's load_dotenv("secrets/.env") runs at import,
    # so a developer's real credentials would otherwise leak into this test and
    # send it down the real-API path.
    monkeypatch.delenv("GOOGLE_CALENDAR_CREDENTIALS", raising=False)
    record = db.save_spec(BOOK_ONLY_SPEC)
    db.set_provider_settings({"book": "google"})
    try:
        response = client.post(
            "/api/realtime/tools/book",
            json={"spec_id": record.id, "lead_id": _first_lead_id(), "args": {}},
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["status"] == "error"
        assert result["outcome"] == "config_error"
    finally:
        db.set_provider_settings({"book": "sim"})  # shared test DB — restore


# --- /api/realtime/web-search -------------------------------------------------


def test_web_search_bridges_responses_api(client, fake_openai_http):
    calls = fake_openai_http(
        {
            "responses": {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Acme raised $40M."}],
                    }
                ]
            }
        }
    )

    response = client.post("/api/realtime/web-search", json={"query": "acme funding"})

    assert response.status_code == 200
    assert response.json() == {"result": "Acme raised $40M."}
    (_, payload), = calls
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["input"] == "acme funding"

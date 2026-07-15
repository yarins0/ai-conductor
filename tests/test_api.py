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


async def _noop_execute_run(run_id, spec, lead, providers=None):
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


def test_stream_run_replays_steps_and_ends_with_done(client):
    # A terminal run: the stream should replay both persisted steps then close
    # with a done frame — this is also what a reconnect rebuilds from.
    spec_record = db.save_spec(SAMPLE_SPEC)
    lead_id = _first_lead_id()
    run = db.create_run(spec_id=spec_record.id, lead_id=lead_id)
    db.add_run_step(run.id, "reach", '{"tool": "reach", "status": "ok", "outcome": "answered", "summary": "picked up", "data": {}}')
    db.add_run_step(run.id, "qualify", '{"tool": "qualify", "status": "ok", "outcome": "qualified", "summary": "score 85", "data": {"intent_score": 85}}')
    db.finish_run(run.id, "completed")

    response = client.get(f"/api/runs/{run.id}/stream")

    assert response.status_code == 200
    body = response.text
    assert '"type": "step"' in body
    assert '"tool": "reach"' in body
    assert '"tool": "qualify"' in body
    assert '"type": "done"' in body
    assert '"status": "completed"' in body


def test_stream_missing_run_returns_404(client):
    response = client.get("/api/runs/999999/stream")
    assert response.status_code == 404


def test_list_leads_returns_seeded_leads(client):
    response = client.get("/api/leads")
    assert response.status_code == 200
    assert len(response.json()) == 3


def test_get_missing_lead_returns_404(client):
    response = client.get("/api/leads/999999")
    assert response.status_code == 404


# --- Live voice/agentic WebSocket ------------------------------------------
#
# LiveSession is faked so the socket plumbing (handshake, event forwarding, the
# turn_done boundary) is tested without an LLM call. The loop itself is covered
# deterministically in test_live_agent.py.


class _FakeLiveSession:
    def __init__(self, spec, lead, run_id, providers=None):
        self.run_id = run_id

    async def handle_turn(self, user_text):
        yield {"type": "assistant", "text": "Sure thing."}
        yield {
            "type": "action",
            "tool": "book",
            "result": {"tool": "book", "status": "ok", "outcome": "booked", "summary": "done", "data": {}},
        }


def test_live_session_ready_forwards_events_and_marks_turn_done(client, monkeypatch):
    monkeypatch.setattr(main.live_agent, "LiveSession", _FakeLiveSession)
    spec_record = db.save_spec(SAMPLE_SPEC)
    lead_id = _first_lead_id()

    with client.websocket_connect(f"/api/live/{spec_record.id}") as websocket:
        websocket.send_json({"type": "start", "lead_id": lead_id, "providers": {}})
        assert websocket.receive_json()["type"] == "ready"

        websocket.send_json({"type": "user", "text": "book me a meeting"})
        assert websocket.receive_json() == {"type": "assistant", "text": "Sure thing."}
        action = websocket.receive_json()
        assert action["type"] == "action" and action["tool"] == "book"
        assert websocket.receive_json() == {"type": "turn_done"}


def test_live_session_missing_lead_sends_error(client):
    spec_record = db.save_spec(SAMPLE_SPEC)
    with client.websocket_connect(f"/api/live/{spec_record.id}") as websocket:
        websocket.send_json({"type": "start", "lead_id": 999999, "providers": {}})
        message = websocket.receive_json()
        assert message["type"] == "error"


# --- Lead CRUD (email/notes, update, delete, search) ------------------------


def test_patch_lead_updates_notes(client):
    lead_id = _first_lead_id()

    response = client.patch(f"/api/leads/{lead_id}", json={"notes": "called back later"})

    assert response.status_code == 200
    assert response.json()["notes"] == "called back later"


def test_patch_missing_lead_returns_404(client):
    response = client.patch("/api/leads/999999", json={"notes": "x"})
    assert response.status_code == 404


def test_delete_lead_then_get_returns_404(client):
    created = client.post(
        "/api/leads", json={"name": "Delete Me", "company": "Gone Inc", "phone": "+1-555-0199"}
    )
    lead_id = created.json()["id"]

    delete_response = client.delete(f"/api/leads/{lead_id}")
    assert delete_response.status_code == 204

    assert client.get(f"/api/leads/{lead_id}").status_code == 404


def test_delete_missing_lead_returns_404(client):
    response = client.delete("/api/leads/999999")
    assert response.status_code == 404


def test_list_leads_filters_by_name_and_company_case_insensitive(client):
    created = client.post(
        "/api/leads", json={"name": "Zelda Zephyr", "company": "Acme Rockets", "phone": "+1-555-0200"}
    )

    by_name = client.get("/api/leads", params={"q": "zelda"})
    assert any(lead["name"] == "Zelda Zephyr" for lead in by_name.json())

    by_company = client.get("/api/leads", params={"q": "ROCKETS"})
    assert any(lead["company"] == "Acme Rockets" for lead in by_company.json())

    no_match = client.get("/api/leads", params={"q": "nonexistent-needle"})
    assert no_match.json() == []

    # Other test modules share this DB and assert an exact seeded-lead count;
    # clean up so this test doesn't leak a row past its own scope.
    client.delete(f"/api/leads/{created.json()['id']}")


# --- Provider settings --------------------------------------------------


def test_get_settings_returns_registry_defaults_on_empty_table(client):
    response = client.get("/api/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["reach"] == "sim"
    assert body["qualify"] == "sim"
    assert body["book"] == "sim"


def test_put_settings_persists_and_survives_fresh_get(client):
    put_response = client.put("/api/settings", json={"reach": "twilio"})

    assert put_response.status_code == 200
    assert put_response.json()["reach"] == "twilio"

    fresh = client.get("/api/settings")
    assert fresh.json()["reach"] == "twilio"


def test_put_settings_unknown_provider_returns_400_and_persists_nothing(client):
    before = client.get("/api/settings").json()

    response = client.put("/api/settings", json={"reach": "not-a-real-provider"})

    assert response.status_code == 400
    # Nothing persisted: settings are exactly what they were before this call
    # (a prior test in this module may have already set a non-default value).
    assert client.get("/api/settings").json() == before

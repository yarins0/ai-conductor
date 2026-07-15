"""Point the Spec Store at a throwaway SQLite file for the whole test session.

app.db.engine is created at import time from the AI_CONDUCTOR_DB env var, so
this must run before anything imports app.db — hence setting it here, at
collection time, ahead of any test module's imports.
"""

import os
import tempfile
from typing import Any

_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()
os.environ["AI_CONDUCTOR_DB"] = "sqlite:///" + _db_file.name.replace("\\", "/")

import anthropic  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402

from app.db import init_db  # noqa: E402  (must follow the env var assignment above)

init_db()


# --- Shared Anthropic fake ---------------------------------------------------
#
# Every LLM-backed path (the Builder, the live agent loop, the simulated call in
# app/sim_lead.py, the qualify scorer) is exercised against canned responses: no
# network, no API key, no cost, and deterministic. Whether the *real* model says
# something good is a model-quality question, answered in the manual demo.


class Block:
    """Stand-in for an Anthropic content block (text or tool_use)."""

    def __init__(self, type: str, text=None, name=None, input=None, id=None) -> None:
        self.type = type
        self.text = text
        self.name = name
        self.input = input
        self.id = id


class Response:
    def __init__(self, content: list[Block]) -> None:
        self.content = content


def text_block(text: str) -> Block:
    return Block("text", text=text)


def tool_block(name: str, id: str, input: dict | None = None) -> Block:
    return Block("tool_use", name=name, id=id, input=input or {})


class FakeMessages:
    def __init__(self, responses: list[Response]) -> None:
        self._responses = responses
        self.calls = 0
        self.requests: list[dict[str, Any]] = []  # every kwargs set, for assertions

    async def create(self, **kwargs: Any) -> Response:
        # Snapshot `messages`: callers grow one list across a loop, so storing the
        # reference would leave every recorded request pointing at the final
        # history instead of what was actually sent at the time.
        self.requests.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        response = self._responses[self.calls]
        self.calls += 1
        return response


class FakeClient:
    def __init__(self, responses: list[Response]) -> None:
        self.messages = FakeMessages(responses)


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch):
    """Install a fake async Anthropic client that replays `responses` in order.

    Patching the `anthropic` module itself reaches every caller at once, since
    each constructs `anthropic.AsyncAnthropic()` at call time rather than holding
    a client from import. Returns the client so tests can inspect `.messages.requests`.
    """

    def _install(responses: list[Response]) -> FakeClient:
        client = FakeClient(responses)
        monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda *a, **k: client)
        return client

    return _install


# --- Shared OpenAI HTTP fake ---------------------------------------------------
#
# The realtime control plane (app/realtime.py) calls OpenAI over raw httpx REST
# (no SDK), so the fake patches httpx.AsyncClient.post itself, keyed by URL
# substring. Same contract as fake_anthropic: no network, no key, no cost. The
# sync TestClient is unaffected (it subclasses httpx.Client, not AsyncClient).


@pytest.fixture
def fake_openai_http(monkeypatch: pytest.MonkeyPatch):
    """Install canned JSON responses for OpenAI REST calls.

    `_install({"client_secrets": {...}, "responses": {...}})` maps a URL
    substring to the JSON body returned for it; an unmapped URL fails the test
    loudly (a silent real call is exactly the bug this exists to prevent).
    Returns a list of (url, json_payload) tuples for request assertions.
    """

    def _install(responses: dict[str, dict]) -> list[tuple[str, dict]]:
        calls: list[tuple[str, dict]] = []

        async def _post(self, url, **kwargs):
            calls.append((str(url), kwargs.get("json", {})))
            for key, body in responses.items():
                if key in str(url):
                    return httpx.Response(200, json=body)
            raise AssertionError(f"unexpected outbound HTTP call in test: {url}")

        monkeypatch.setattr(httpx.AsyncClient, "post", _post)
        return calls

    return _install

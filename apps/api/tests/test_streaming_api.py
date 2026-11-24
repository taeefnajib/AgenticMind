"""Streaming through the API (ADR-0040): token events in run streams and the one-call streaming endpoint."""

from __future__ import annotations

import json

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_runtime.secrets import MappingSecretResolver
from fastapi.testclient import TestClient

GRAPH = {
    "ir_version": "1.0",
    "project": {"id": "template", "name": "Streamer"},
    "nodes": [
        {
            "id": "q",
            "type": "agenticmind.core.manual_input",
            "version": "1.0.0",
            "config": {"key": "q", "value_type": "String", "default": "tell me a long story about streaming tokens"},
        },
        {
            "id": "llm",
            "type": "agenticmind.models.llm",
            "version": "1.2.0",
            "config": {"provider": "mock", "model": "m"},
        },
        {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "answer"}},
    ],
    "edges": [
        {
            "id": "e1",
            "kind": "data",
            "source": {"node": "q", "port": "value"},
            "target": {"node": "llm", "port": "prompt"},
        },
        {
            "id": "e2",
            "kind": "data",
            "source": {"node": "llm", "port": "text"},
            "target": {"node": "out", "port": "value"},
        },
    ],
    "metadata": {},
}


def parse(raw: str) -> list[tuple[str, dict]]:
    out = []
    for block in raw.split("\n\n"):
        name = next((ln[7:] for ln in block.split("\n") if ln.startswith("event: ")), None)
        data = next((ln[6:] for ln in block.split("\n") if ln.startswith("data: ")), None)
        if name and data:
            out.append((name, json.loads(data)))
    return out


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode="off",
        mcp_server=False,
    )
    app = create_app(settings, secrets=MappingSecretResolver({}))
    with TestClient(app) as c:
        yield c


def test_streaming_endpoint_and_token_events(client):
    pid = client.post("/api/projects", json={"name": "S", "document": GRAPH}).json()["id"]
    with client.stream("POST", f"/api/projects/{pid}/runs/stream?trace=false", json={"inputs": {}}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = parse("".join(resp.iter_text()))
    names = [n for n, _ in events]
    assert names[0] == "run" and names[-2:] == ["result", "end"] and "trace" not in names
    tokens = [d for n, d in events if n == "token"]
    result = events[-2][1]
    assert result["status"] == "succeeded" and tokens and all(t["node_id"] == "llm" for t in tokens)
    assert "".join(t["text"] for t in tokens) == result["outputs"]["answer"]  # the deltas add up to the output
    assert [t["seq"] for t in tokens] == sorted(t["seq"] for t in tokens)

    # the run's event stream replays its tokens (with traces) after the fact, and can leave them out
    run_id = events[0][1]["run_id"]
    with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Accept": "text/event-stream"}) as resp:
        replay = parse("".join(resp.iter_text()))
    assert {n for n, _ in replay} >= {"token", "trace", "end"}
    with client.stream("GET", f"/api/runs/{run_id}/events?tokens=false", headers={"Accept": "text/event-stream"}) as r:
        assert "token" not in {n for n, _ in parse("".join(r.iter_text()))}

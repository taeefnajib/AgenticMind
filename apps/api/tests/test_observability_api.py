"""Observability in the API: metrics, OTLP download, automatic export after runs, settings, metrics token."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_runtime.secrets import MappingSecretResolver
from fastapi.testclient import TestClient

GRAPH = {
    "ir_version": "1.0",
    "project": {"id": "template", "name": "Observed"},
    "nodes": [
        {
            "id": "q",
            "type": "agenticmind.core.manual_input",
            "version": "1.0.0",
            "config": {"key": "q", "value_type": "String", "default": "hello"},
        },
        {
            "id": "llm",
            "type": "agenticmind.models.llm",
            "version": "1.2.0",
            "config": {"provider": "mock", "model": "mock-1"},
            "ui": {"label": "Writer"},
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


class Collector:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={})


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode="off",
        mcp_server=False,
        metrics_token="fixture-metrics-token",
    )
    app = create_app(settings, secrets=MappingSecretResolver({"OTEL_HEADERS": "x-api-key=fixture-otel"}))
    collector = Collector()
    with TestClient(app) as client:
        app.state.services.observability.transport = httpx.MockTransport(collector.handler)
        pid = client.post("/api/projects", json={"name": "Observed", "document": GRAPH}).json()["id"]
        yield client, app.state.services, collector, pid


def run(client, pid):
    run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
    for _ in range(200):
        if client.get(f"/api/runs/{run_id}").json()["status"] in ("succeeded", "failed"):
            return run_id
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_metrics_and_trace_download(env):
    client, _svc, _c, pid = env
    run_id = run(client, pid)
    assert client.get("/api/metrics").status_code == 401  # token configured
    text = client.get("/api/metrics", headers={"Authorization": "Bearer fixture-metrics-token"}).text
    assert 'agenticmind_runs_total{status="succeeded"} 1' in text
    assert 'agenticmind_model_calls_total{provider="mock",model="mock-1"} 1' in text
    body = client.get(f"/api/runs/{run_id}/otlp").json()
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert {s["name"] for s in spans} >= {"invoke_workflow Observed", "chat mock-1"}
    assert client.get("/api/runs/run_missing/otlp").status_code == 404


def test_settings_and_automatic_export(env):
    client, svc, collector, pid = env
    state = client.get("/api/settings/observability").json()
    assert state["source"] == "off" and state["effective"] is None
    bad = client.put("/api/settings/observability", json={"endpoint": "localhost"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "endpoint"
    ok = client.put(
        "/api/settings/observability",
        json={"endpoint": "http://collector:4318", "protocol": "http/json", "headers_secret": "OTEL_HEADERS"},
    ).json()
    assert ok["source"] == "workspace" and ok["effective"]["url"] == "http://collector:4318/v1/traces"
    assert ok["effective"]["header_names"] == ["x-api-key"] and "fixture-otel" not in json.dumps(ok)  # names only
    assert client.post("/api/settings/observability/test").json()["ok"] is True
    run_id = run(client, pid)
    for _ in range(100):
        if svc.observability.status["last_run_id"] == run_id:
            break
        time.sleep(0.05)
    exported = json.loads(collector.requests[-1].content)
    assert collector.requests[-1].headers["x-api-key"] == "fixture-otel"
    assert exported["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
    assert svc.observability.status["exported"] == 1 and svc.observability.status["last_error"] is None
    assert client.post(f"/api/runs/{run_id}/export").json() == {"ok": True, "spans": 4}
    off = client.put("/api/settings/observability", json={"endpoint": "http://collector:4318", "enabled": False}).json()
    assert off["source"] == "off"
    assert client.post(f"/api/runs/{run_id}/export").status_code == 502
    assert client.delete("/api/settings/observability").json()["settings"] is None

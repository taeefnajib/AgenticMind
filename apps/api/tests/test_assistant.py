"""AI workflow designer (ADR-0022): proposals are validated, repaired from diagnostics, laid out and diffed."""

from __future__ import annotations

import dataclasses
import json

import pytest
from agenticmind_api.assistant import catalog_text, diff_graphs, extract_json, layout
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_node_sdk import ModelUsage
from agenticmind_providers import ChatResponse, register_provider
from agenticmind_registry import default_registry
from agenticmind_runtime import MappingSecretResolver
from fastapi.testclient import TestClient


class ScriptedProvider:
    """Returns queued replies and records every request it received."""

    name = "scripted-test"

    def __init__(self) -> None:
        self.replies: list[str] = []
        self.requests: list = []
        self.keys: list = []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        self.keys.append(api_key)
        text = self.replies.pop(0)
        return ChatResponse(text=text, usage=ModelUsage(provider=self.name, model=request.model, output_tokens=5))


@pytest.fixture
def scripted():
    p = ScriptedProvider()
    register_provider(p)
    return p


def _client(tmp_path, *, provider="scripted-test", secrets=None):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        assistant_provider=provider,
        assistant_model="test-model",
        assistant_api_key_secret="TEST_ASSISTANT_KEY",
    )
    return TestClient(create_app(settings, secrets=MappingSecretResolver(secrets or {})))


def yes_no_graph(llm_target_port="prompt"):
    return {
        "name": "Yes or no answers",
        "description": "Answers any question with yes or no.",
        "nodes": [
            {"id": "question", "type": "agenticmind.core.manual_input", "version": "1.0.0",
             "config": {"key": "question", "value_type": "String"}},
            {"id": "answer_llm", "type": "agenticmind.models.llm", "version": "1.1.0",
             "config": {"provider": "mock", "model": "mock-1",
                        "system_prompt": "Answer only with yes or no.", "max_tokens": 16}},
            {"id": "answer", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "answer"}},
        ],
        "edges": [
            {"id": "e1", "kind": "data", "source": {"node": "question", "port": "value"},
             "target": {"node": "answer_llm", "port": llm_target_port}},
            {"id": "e2", "kind": "data", "source": {"node": "answer_llm", "port": "text"},
             "target": {"node": "answer", "port": "value"}},
        ],
    }  # fmt: skip


def reply(change, text="Built it."):
    return json.dumps({"reply": text, "change": change})


def blank_project(client):
    r = client.post("/api/projects", json={"name": "Untitled"})
    assert r.status_code == 201
    return r.json()


def test_creates_a_workflow_in_a_blank_project(tmp_path, scripted):
    client = _client(tmp_path, secrets={"TEST_ASSISTANT_KEY": "fixture-key"})
    project = blank_project(client)
    scripted.replies = [reply(yes_no_graph())]
    r = client.post(f"/api/projects/{project['id']}/assistant",
                    json={"message": "Make a workflow that answers any question with yes or no"})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is True and body["applicable"] is True and body["attempts"] == 1
    doc = body["document"]
    assert doc["project"]["id"] == project["id"] and doc["project"]["name"] == "Yes or no answers"
    assert [n["id"] for n in doc["nodes"]] == ["question", "answer_llm", "answer"]
    assert doc["nodes"][1]["config"]["max_tokens"] == 512  # tiny output budgets are raised
    assert [n["ui"]["position"]["x"] for n in doc["nodes"]] == [0, 320, 640]  # columns by depth
    assert {a["id"] for a in body["changes"]["added"]} == {"question", "answer_llm", "answer"}
    # the model saw the catalog, the rules and the (empty) current graph; the key came from the secret
    request = scripted.requests[0]
    assert "agenticmind.models.llm@1.1.0" in request.system and "$secret" in request.system
    assert '"nodes": []' in request.messages[-1].content
    assert scripted.keys == ["fixture-key"]
    # nothing was saved: the client applies the proposal
    assert client.get(f"/api/projects/{project['id']}").json()["revision"] == project["revision"]
    # and the proposal is a runnable project
    saved = client.put(f"/api/projects/{project['id']}", json={"document": doc}).json()
    run = client.post(f"/api/projects/{project['id']}/runs", json={"inputs": {"question": "Is water wet?"}})
    assert run.status_code in (200, 201, 202), run.text
    assert saved["revision"] == project["revision"] + 1


def test_repairs_from_validation_diagnostics(tmp_path, scripted):
    client = _client(tmp_path)
    project = blank_project(client)
    scripted.replies = [reply(yes_no_graph(llm_target_port="question")), reply(yes_no_graph(), "Fixed.")]
    body = client.post(f"/api/projects/{project['id']}/assistant", json={"message": "yes/no bot"}).json()
    assert body["attempts"] == 2 and body["applicable"] is True and body["reply"] == "Fixed."
    feedback = scripted.requests[1].messages[-1].content
    assert "failed validation" in feedback and "EDGE_UNKNOWN_PORT" in feedback


def test_recovers_from_non_json_and_reports_unfixable_graphs(tmp_path, scripted):
    client = _client(tmp_path)
    project = blank_project(client)
    bad = reply(yes_no_graph(llm_target_port="question"))
    scripted.replies = ["Sure! Here is the plan...", bad, bad]
    body = client.post(f"/api/projects/{project['id']}/assistant", json={"message": "yes/no bot"}).json()
    assert body["attempts"] == 3
    assert "no JSON object" in scripted.requests[1].messages[-1].content
    assert body["applicable"] is False and body["document"] is not None
    assert any(d["code"] == "EDGE_UNKNOWN_PORT" for d in body["diagnostics"])


def test_question_without_change(tmp_path, scripted):
    client = _client(tmp_path)
    project = blank_project(client)
    scripted.replies = [reply(None, "Which output format do you want?")]
    body = client.post(f"/api/projects/{project['id']}/assistant", json={"message": "help"}).json()
    assert body["document"] is None and body["reply"] == "Which output format do you want?"


def test_changes_an_existing_project_and_keeps_positions(tmp_path, scripted):
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "Assistant", "template": "mock-llm-assistant"}).json()
    current = project["document"]
    nodes = [{k: n[k] for k in ("id", "type", "version", "config")} for n in current["nodes"]]
    llm = next(n for n in nodes if n["id"] == "llm")
    llm["config"] = {**llm["config"], "system_prompt": "Answer only with yes or no."}
    nodes.append({"id": "shout", "type": "agenticmind.core.text_transform", "version": "1.0.0",
                  "config": {"operation": "uppercase"}})  # fmt: skip

    def data(eid, src, sport, dst, dport):
        return {
            "id": eid,
            "kind": "data",
            "source": {"node": src, "port": sport},
            "target": {"node": dst, "port": dport},
        }

    edges = [e for e in current["edges"] if e["id"] != "e3"] + [
        data("e4", "llm", "text", "shout", "text"),
        data("e5", "shout", "text", "answer", "value"),
    ]
    scripted.replies = [reply({"nodes": nodes, "edges": edges})]
    edited = dict(current)
    edited["project"] = {**current["project"], "name": "Renamed on the canvas"}  # unsaved edit is the base
    body = client.post(f"/api/projects/{project['id']}/assistant",
                       json={"message": "answer yes/no in capitals", "document": edited,
                             "history": [{"role": "user", "content": "earlier"},
                                         {"role": "assistant", "content": "ok"}]}).json()  # fmt: skip
    assert body["applicable"] is True, body["diagnostics"]
    assert body["document"]["project"]["name"] == "Renamed on the canvas"
    before = {n["id"]: n["ui"]["position"] for n in current["nodes"]}
    after = {n["id"]: n["ui"]["position"] for n in body["document"]["nodes"]}
    assert all(after[i] == before[i] for i in before)
    assert after["shout"]["y"] > max(p["y"] for p in before.values())
    assert body["changes"]["added"] == [{"id": "shout", "type": "agenticmind.core.text_transform"}]
    assert body["changes"]["modified"] == [{"id": "llm", "type": "agenticmind.models.llm"}]
    assert body["changes"]["removed"] == [] and body["changes"]["edges_added"] == 2
    sent = [m.content for m in scripted.requests[0].messages]
    assert sent[:2] == ["earlier", "ok"] and "Renamed on the canvas" in sent[-1]


def test_openrouter_needs_its_secret(tmp_path):
    client = _client(tmp_path, provider="openrouter")
    assert client.get("/api/assistant").json() == {
        "provider": "openrouter", "model": "test-model", "secret": "TEST_ASSISTANT_KEY", "configured": False,
    }  # fmt: skip
    project = blank_project(client)
    r = client.post(f"/api/projects/{project['id']}/assistant", json={"message": "hi"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "ASSISTANT_NOT_CONFIGURED"
    assert "TEST_ASSISTANT_KEY" in r.json()["detail"]["message"]
    assert client.post("/api/projects/nope/assistant", json={"message": "hi"}).status_code == 404


def test_catalog_lists_latest_permitted_versions(monkeypatch):
    registry = default_registry()
    text = catalog_text(registry)
    assert "agenticmind.models.llm@1.1.0" in text and "agenticmind.models.llm@1.0.0" not in text
    assert "control branches: true, false" in text  # conditional
    entries = [
        dataclasses.replace(e, permitted=e.manifest.type != "agenticmind.core.output") for e in registry.entries()
    ]
    monkeypatch.setattr(registry, "entries", lambda: entries)
    assert "agenticmind.core.output@" not in catalog_text(registry)


def test_helpers():
    assert extract_json('```json\n{"reply": "x", "change": null}\n```') == {"reply": "x", "change": None}
    assert extract_json('Here you go: {"a": 1} thanks')["a"] == 1
    with pytest.raises(ValueError):
        extract_json("no json")
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    edges = [{"source": {"node": "a"}, "target": {"node": "c"}}, {"source": {"node": "b"}, "target": {"node": "c"}}]
    layout(nodes, edges, {"nodes": []})
    assert [n["ui"]["position"] for n in nodes] == [{"x": 0, "y": 0}, {"x": 0, "y": 170}, {"x": 320, "y": 0}]
    d = diff_graphs({"nodes": [{"id": "a", "type": "t"}], "edges": []}, {"nodes": [], "edges": []})
    assert d["removed"] == [{"id": "a", "type": "t"}]

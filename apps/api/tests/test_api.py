import json
import time

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_api.templates import list_templates, load_template
from agenticmind_registry import default_registry
from agenticmind_runtime import validate_project
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}", data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        yield c


def wait_for_run(client, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("succeeded", "failed", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_templates_are_valid():
    registry = default_registry()
    for t in list_templates():
        _doc, report = validate_project(load_template(t["name"]), registry)
        assert report.valid, (t["name"], report.diagnostics)
        assert not report.diagnostics, (t["name"], report.diagnostics)


def test_registry_endpoints(client):
    assert client.get("/api/health").json()["status"] == "ok"
    nodes = client.get("/api/nodes").json()
    output = next(n for n in nodes if n["type"] == "agenticmind.core.output")
    assert output["trust"] == "core" and output["inputs"][0]["name"] == "value"
    assert client.get("/api/ir/schema").json()["title"].startswith("AgenticMind IR")


def test_create_save_load_validate_export(client):
    created = client.post("/api/projects", json={"name": "Demo"}).json()
    pid = created["id"]
    assert created["revision"] == 1 and created["document"]["nodes"] == []

    doc = load_template("hello-pipeline")
    saved = client.put(f"/api/projects/{pid}", json={"document": doc, "expected_revision": 1})
    assert saved.status_code == 200
    body = saved.json()
    assert body["revision"] == 2
    assert body["document"]["project"]["id"] == pid  # forced to match resource

    # no-op save does not create a revision
    again = client.put(f"/api/projects/{pid}", json={"document": body["document"]}).json()
    assert again["revision"] == 2
    # stale revision conflicts
    assert client.put(f"/api/projects/{pid}", json={"document": doc, "expected_revision": 1}).status_code == 409

    loaded = client.get(f"/api/projects/{pid}").json()
    assert {n["id"] for n in loaded["document"]["nodes"]} == {"name", "greet", "shout", "result"}
    assert [r["revision"] for r in client.get(f"/api/projects/{pid}/revisions").json()] == [2, 1]

    assert client.post(f"/api/projects/{pid}/validate").json() == {"valid": True, "diagnostics": []}

    unsaved = json.loads(json.dumps(loaded["document"]))
    unsaved["edges"] = unsaved["edges"][:1]
    report = client.post(f"/api/projects/{pid}/validate", json={"document": unsaved}).json()
    assert not report["valid"]
    errors = {d["code"] for d in report["diagnostics"] if d["severity"] == "error"}
    assert errors == {"PORT_MISSING_REQUIRED_INPUT"}

    exported = client.get(f"/api/projects/{pid}/export")
    assert exported.headers["content-disposition"].endswith('.agenticmind.json"')
    assert exported.headers["x-agenticmind-content-hash"] == loaded["content_hash"]
    reimported = client.post("/api/projects", json={"name": "Copy", "document": json.loads(exported.text)})
    assert reimported.status_code == 201


def test_schema_invalid_save_rejected(client):
    pid = client.post("/api/projects", json={"name": "X"}).json()["id"]
    bad = {"ir_version": "1.0", "project": {"name": "X"}, "nodes": [{"id": "1bad", "type": "x"}]}
    resp = client.put(f"/api/projects/{pid}", json={"document": bad})
    assert resp.status_code == 422
    assert resp.json()["detail"]["diagnostics"][0]["code"] == "IR_SCHEMA_VIOLATION"


def test_run_lifecycle_events_and_outputs(client):
    pid = client.post("/api/projects", json={"name": "Hello", "template": "hello-pipeline"}).json()["id"]
    run = client.post(f"/api/projects/{pid}/runs", json={"inputs": {"name": "Ada"}})
    assert run.status_code == 202
    run_id = run.json()["id"]
    final = wait_for_run(client, run_id)
    assert final["status"] == "succeeded"
    assert final["outputs"] == {"greeting": "HELLO, ADA! WELCOME TO AGENTICMIND."}
    assert final["node_status"]["shout"] == "succeeded"

    events = client.get(f"/api/runs/{run_id}/events").json()
    assert events[0]["event_type"] == "run_started" and events[-1]["event_type"] == "run_succeeded"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    later = client.get(f"/api/runs/{run_id}/events", params={"after": 3}).json()
    assert later[0]["seq"] == 4

    outs = client.get(f"/api/runs/{run_id}/nodes/greet/outputs").json()
    assert outs["outputs"] == {"text": "Hello, Ada! Welcome to AgenticMind."}
    assert len(client.get(f"/api/projects/{pid}/runs").json()) == 1


def test_sse_stream_replays_and_resumes(client):
    pid = client.post("/api/projects", json={"name": "Hello", "template": "hello-pipeline"}).json()["id"]
    run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
    wait_for_run(client, run_id)
    total = len(client.get(f"/api/runs/{run_id}/events").json())

    with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Accept": "text/event-stream"}) as resp:
        text = "".join(resp.iter_text())
    assert text.count("event: trace") == total and "event: end" in text

    headers = {"Accept": "text/event-stream", "Last-Event-ID": str(total - 2)}
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as resp:
        text = "".join(resp.iter_text())
    assert text.count("event: trace") == 2


def test_invalid_graph_cannot_run(client):
    pid = client.post("/api/projects", json={"name": "Bad"}).json()["id"]
    doc = load_template("hello-pipeline")
    doc["edges"] = []
    client.put(f"/api/projects/{pid}", json={"document": doc})
    resp = client.post(f"/api/projects/{pid}/runs", json={})
    assert resp.status_code == 422
    assert resp.json()["detail"]["valid"] is False


def test_cancel_run(client):
    pid = client.post("/api/projects", json={"name": "Slow"}).json()["id"]
    doc = {
        "ir_version": "1.0",
        "project": {"id": pid, "name": "Slow"},
        "nodes": [{"id": "wait", "type": "agenticmind.core.delay", "version": "1.0.0", "config": {"seconds": 10}}],
        "edges": [],
    }
    client.put(f"/api/projects/{pid}", json={"document": doc})
    run_id = client.post(f"/api/projects/{pid}/runs", json={}).json()["id"]
    time.sleep(0.2)
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    final = wait_for_run(client, run_id)
    assert final["status"] == "cancelled"
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409


def test_failed_run_reports_node_error(client):
    pid = client.post("/api/projects", json={"name": "Req"}).json()["id"]
    doc = load_template("hello-pipeline")
    doc["nodes"][0]["config"] = {"key": "name", "required": True}  # no default
    client.put(f"/api/projects/{pid}", json={"document": doc})
    run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
    final = wait_for_run(client, run_id)
    assert final["status"] == "failed"
    assert final["error"]["code"] == "INPUT_MISSING" and final["error"]["node_id"] == "name"


def test_delete_project(client):
    pid = client.post("/api/projects", json={"name": "Gone", "template": "hello-pipeline"}).json()["id"]
    run_id = client.post(f"/api/projects/{pid}/runs", json={}).json()["id"]
    wait_for_run(client, run_id)
    assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_ticket_triage_template_runs(client):
    pid = client.post("/api/projects", json={"name": "Triage", "template": "ticket-triage"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/validate").json()["valid"] is True
    run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": {}}).json()["id"]
    final = wait_for_run(client, run_id)
    assert final["status"] == "succeeded", final["error"]
    replies = final["outputs"]["replies"]
    assert [r.split("]")[0] + "]" for r in replies] == ["[billing]", "[technical]", "[general]", "[billing]"]
    assert final["outputs"]["loop_report"]["iterations"] == 4
    events = client.get(f"/api/runs/{run_id}/events").json()
    route_iterations = sorted(
        e["data"]["iteration"][0] for e in events if e.get("node_id") == "route" and e["event_type"] == "node_succeeded"
    )
    assert route_iterations == [0, 1, 2, 3]


def _memory_project(client):
    pid = client.post("/api/projects", json={"name": "Mem", "template": "assistant-with-memory"}).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    agent = next(n for n in doc["nodes"] if n["id"] == "agent")
    agent["config"].update(provider="mock", model="mock-1")
    agent["config"].pop("api_key")
    assert client.put(f"/api/projects/{pid}", json={"document": doc}).status_code == 200
    return pid


def _ask(client, pid, **inputs):
    run_id = client.post(f"/api/projects/{pid}/runs", json={"inputs": inputs, "targets": ["answer"]}).json()["id"]
    final = wait_for_run(client, run_id)
    assert final["status"] == "succeeded", final["error"]
    return final["outputs"]["answer"]


def test_memory_template_conversation_consent_and_deletion(client):
    pid = _memory_project(client)
    _ask(client, pid, question="hello there", conversation_id="chat-1")
    _ask(client, pid, question="second question", conversation_id="chat-1")
    convs = client.get(f"/api/projects/{pid}/conversations").json()
    assert [(c["id"], c["messages"]) for c in convs] == [("chat-1", 4)]
    turns = client.get(f"/api/projects/{pid}/conversations/chat-1").json()
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert turns[2]["content"] == "second question"

    # no consent -> the save tool refuses and nothing is stored
    answer = _ask(client, pid, question="save_memory: I prefer green tea", conversation_id="chat-2")
    assert answer["steps"][0]["ok"] is False
    assert client.get(f"/api/projects/{pid}/memories").json() == []
    # with consent -> stored, listed with its source run
    answer = _ask(
        client, pid, question="save_memory: I prefer green tea", conversation_id="chat-2", memory_consent=True
    )
    assert answer["steps"][0]["ok"] is True
    memories = client.get(f"/api/projects/{pid}/memories").json()
    assert [m["content"] for m in memories] == ["I prefer green tea"]
    assert memories[0]["source"]["run_id"] and memories[0]["expires_at"]
    # recall in a new conversation finds it
    answer = _ask(client, pid, question="recall_memory: green tea", conversation_id="chat-3")
    assert "green tea" in answer["answer"]

    # projects are isolated
    other = _memory_project(client)
    assert client.get(f"/api/projects/{other}/memories").json() == []
    # deletion
    assert client.delete(f"/api/projects/{pid}/memories/{memories[0]['id']}").status_code == 204
    assert client.get(f"/api/projects/{pid}/memories").json() == []
    assert client.delete(f"/api/projects/{pid}/conversations/chat-1").json() == {"deleted": 4}
    assert client.get(f"/api/projects/{pid}/conversations/bad%20id").status_code == 400
    assert client.delete(f"/api/projects/{pid}/memories/mem_missing").status_code == 404

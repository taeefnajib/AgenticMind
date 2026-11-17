import time
from pathlib import Path

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from fastapi.testclient import TestClient

HANDBOOK = (Path(__file__).resolve().parents[3] / "examples" / "data" / "employee-handbook.md").read_bytes()


@pytest.fixture
def client(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}", data_dir=tmp_path / "data")
    with TestClient(create_app(settings)) as c:
        yield c


def wait(client, run_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] in ("succeeded", "failed", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def make_project(client):
    pid = client.post("/api/projects", json={"name": "RAG", "template": "rag-retrieval"}).json()["id"]
    file_id = client.post(
        f"/api/projects/{pid}/files", files={"file": ("employee-handbook.md", HANDBOOK, "text/markdown")}
    ).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    nodes = {n["id"]: n for n in doc["nodes"]}
    nodes["load"]["config"]["files"] = [file_id]
    nodes["embedding"]["config"] = {"provider": "hash", "model": "hash", "dimensions": 384, "batch_size": 32}
    assert client.put(f"/api/projects/{pid}", json={"document": doc}).status_code == 200
    return pid


def test_template_uses_local_defaults():
    from agenticmind_api.templates import load_template

    nodes = {n["id"]: n for n in load_template("rag-retrieval")["nodes"]}
    assert nodes["embedding"]["config"]["provider"] == "fastembed"
    assert nodes["store"]["config"]["backend"] == "qdrant"


def test_index_then_query_with_run_targets(client, tmp_path):
    pid = make_project(client)

    run = client.post(f"/api/projects/{pid}/runs", json={"targets": ["index_report"]}).json()
    final = wait(client, run["id"])
    assert final["status"] == "succeeded", final["error"]
    report = final["outputs"]["index_report"]
    assert report["embedded"] == report["documents"] > 5 and report["collection"] == f"{pid}__documents"
    assert "question" not in final["node_status"]  # query flow was not executed
    events = client.get(f"/api/runs/{run['id']}/events").json()
    assert events[0]["data"]["targets"] == ["index_report"]

    run = client.post(
        f"/api/projects/{pid}/runs",
        json={"targets": ["results"], "inputs": {"question": "How many days of annual leave do employees get?"}},
    ).json()
    final = wait(client, run["id"])
    assert final["status"] == "succeeded", final["error"]
    results = final["outputs"]["results"]
    assert len(results) == 4 and "load" not in final["node_status"]
    assert any("25 days" in r["content"] for r in results)
    assert all(r["retrieval_method"] == "vector" for r in results)

    assert (tmp_path / "data" / "vectors" / "qdrant").is_dir()
    client.delete(f"/api/projects/{pid}")
    from agenticmind_vectorstores import StoreSpec, open_store

    store = open_store(
        StoreSpec(backend="qdrant", collection=f"{pid}__documents", local_path=tmp_path / "data" / "vectors" / "qdrant")
    )
    assert not store.exists()


def test_unknown_target_is_rejected(client):
    pid = make_project(client)
    resp = client.post(f"/api/projects/{pid}/runs", json={"targets": ["ghost"]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["diagnostics"][0]["code"] == "RUN_TARGET_UNKNOWN"


def test_answers_template_end_to_end_with_citations(client):
    pid = client.post("/api/projects", json={"name": "Answers", "template": "rag-answers"}).json()["id"]
    file_id = client.post(
        f"/api/projects/{pid}/files", files={"file": ("employee-handbook.md", HANDBOOK, "text/markdown")}
    ).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    nodes = {n["id"]: n for n in doc["nodes"]}
    assert nodes["llm"]["config"]["api_key"] == {"$secret": "OPENROUTER_API_KEY"}  # template ships a reference only
    nodes["load"]["config"]["files"] = [file_id]
    nodes["embedding"]["config"] = {"provider": "hash", "model": "hash", "dimensions": 384, "batch_size": 32}
    nodes["llm"]["config"] = {"provider": "mock", "model": "mock-1", "temperature": 0, "max_tokens": 200}
    assert client.put(f"/api/projects/{pid}", json={"document": doc}).status_code == 200

    final = wait(client, client.post(f"/api/projects/{pid}/runs", json={"targets": ["index_report"]}).json()["id"])
    assert final["status"] == "succeeded", final["error"]

    run = client.post(
        f"/api/projects/{pid}/runs",
        json={"targets": ["answer"], "inputs": {"question": "How many days of annual leave do employees get?"}},
    ).json()
    final = wait(client, run["id"])
    assert final["status"] == "succeeded", final["error"]
    result = final["outputs"]["answer"]
    assert result["answer"].startswith("[mock:mock-1]") and result["answer"].endswith("[1]")
    assert result["validation"] == {
        "has_citations": True,
        "cited": [1],
        "invalid": [],
        "uncited": result["validation"]["uncited"],
    }
    cited = result["citations"][0]
    assert cited["n"] == 1 and cited["filename"] == "employee-handbook.md" and "Annual leave" in cited["section"]
    assert set(final["node_status"]) == {
        "embedding",
        "store",
        "question",
        "retrieve",
        "context",
        "llm",
        "cite",
        "answer",
    }


def test_agent_template_end_to_end(client):
    pid = client.post("/api/projects", json={"name": "Agent", "template": "agent-with-tools"}).json()["id"]
    file_id = client.post(
        f"/api/projects/{pid}/files", files={"file": ("employee-handbook.md", HANDBOOK, "text/markdown")}
    ).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    nodes = {n["id"]: n for n in doc["nodes"]}
    assert nodes["agent"]["config"]["api_key"] == {"$secret": "OPENROUTER_API_KEY"}
    nodes["load"]["config"]["files"] = [file_id]
    nodes["embedding"]["config"] = {"provider": "hash", "model": "hash", "dimensions": 384, "batch_size": 32}
    nodes["agent"]["config"] = {"provider": "mock", "model": "mock-1"}
    assert client.put(f"/api/projects/{pid}", json={"document": doc}).status_code == 200
    assert client.post(f"/api/projects/{pid}/validate").json()["valid"] is True

    final = wait(client, client.post(f"/api/projects/{pid}/runs", json={"targets": ["index_report"]}).json()["id"])
    assert final["status"] == "succeeded", final["error"]
    run = client.post(
        f"/api/projects/{pid}/runs",
        json={"targets": ["answer"], "inputs": {"question": "calculator: 25 * 3"}},
    ).json()
    final = wait(client, run["id"])
    assert final["status"] == "succeeded", final["error"]
    result = final["outputs"]["answer"]
    assert result["steps"][0]["tool"] == "calculator" and result["steps"][0]["ok"] is True
    assert "75" in result["answer"] and result["report"]["tool_calls"] == 1
    events = client.get(f"/api/runs/{run['id']}/events").json()
    assert any(e.get("data", {}).get("type") == "tool_call" for e in events)

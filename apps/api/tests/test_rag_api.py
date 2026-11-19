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
    client.delete(f"/api/projects/{pid}?confirm=DELETE")
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


def test_advanced_rag_template_runs_offline(client):
    pid = client.post("/api/projects", json={"name": "Adv", "template": "advanced-rag"}).json()["id"]
    file_id = client.post(
        f"/api/projects/{pid}/files", files={"file": ("employee-handbook.md", HANDBOOK, "text/markdown")}
    ).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    nodes = {n["id"]: n for n in doc["nodes"]}
    nodes["load"]["config"]["files"] = [file_id]
    nodes["embedding"]["config"] = {"provider": "hash", "model": "hash", "dimensions": 384, "batch_size": 32}
    nodes["rewrite"]["config"].update(llm_provider="mock", llm_model="mock-1", variants=0)
    nodes["rewrite"]["config"].pop("api_key")
    nodes["rerank"]["config"]["provider"] = "overlap"  # avoid downloading the cross-encoder in CI
    nodes["llm"]["config"].update(provider="mock", model="mock-1")
    nodes["llm"]["config"].pop("api_key")
    assert client.put(f"/api/projects/{pid}", json={"document": doc}).status_code == 200

    indexed = wait(client, client.post(f"/api/projects/{pid}/runs", json={"targets": ["index_report"]}).json()["id"])
    assert indexed["status"] == "succeeded", indexed["error"]
    assert indexed["outputs"]["index_report"]["keyword_indexed"] == indexed["outputs"]["index_report"]["documents"]

    run_id = client.post(
        f"/api/projects/{pid}/runs",
        json={"inputs": {"question": "How many days of annual leave do employees get?"}, "targets": ["answer"]},
    ).json()["id"]
    final = wait(client, run_id)
    assert final["status"] == "succeeded", final["error"]
    events = client.get(f"/api/runs/{run_id}/events").json()
    kinds = {(e.get("node_id"), (e.get("data") or {}).get("type")) for e in events}
    assert {("hybrid", "retrieved"), ("rerank", "reranked"), ("compress", "compressed")} <= kinds
    answer = final["outputs"]["answer"]
    assert answer["citations"] and "leave" in answer["citations"][0]["excerpt"].lower()


def wait_eval(client, eval_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ev = client.get(f"/api/evaluations/{eval_id}").json()
        if ev["status"] in ("succeeded", "failed", "cancelled"):
            return ev
        time.sleep(0.1)
    raise AssertionError("evaluation did not finish")


CASES = [
    {
        "id": "leave",
        "inputs": {"question": "How many days of annual leave do employees get?"},
        "expected_sources": ["employee-handbook.md#annual leave"],
        "must_include": ["25"],
    },
    {
        "id": "hotel",
        "inputs": {"question": "What is the hotel limit per night?"},
        "expected_sources": ["employee-handbook.md#travel"],
        "must_include": ["180"],
    },
    {
        "id": "phishing",
        "inputs": {"question": "Who do I report phishing emails to?"},
        "expected_sources": ["employee-handbook.md#security"],
    },
]


def test_evaluation_lab_two_revisions_compare_and_audit(client):
    pid = client.post("/api/projects", json={"name": "Eval", "template": "rag-answers"}).json()["id"]
    file_id = client.post(
        f"/api/projects/{pid}/files", files={"file": ("employee-handbook.md", HANDBOOK, "text/markdown")}
    ).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    nodes = {n["id"]: n for n in doc["nodes"]}
    nodes["load"]["config"]["files"] = [file_id]
    nodes["embedding"]["config"] = {"provider": "hash", "model": "hash", "dimensions": 384, "batch_size": 32}
    nodes["llm"]["config"].update(provider="mock", model="mock-1")
    nodes["llm"]["config"].pop("api_key")
    rev_a = client.put(f"/api/projects/{pid}", json={"document": doc}).json()["revision"]
    assert (
        wait(client, client.post(f"/api/projects/{pid}/runs", json={"targets": ["index_report"]}).json()["id"])[
            "status"
        ]
        == "succeeded"
    )

    # dataset: create, edit (new version), unchanged save keeps the version, invalid rejected
    ds = client.post(f"/api/projects/{pid}/datasets", json={"name": "Handbook QA", "cases": CASES[:2]}).json()
    assert ds["version"] == 1 and ds["cases_count"] == 2
    v2 = client.put(f"/api/datasets/{ds['id']}", json={"name": "Handbook QA", "cases": CASES}).json()
    assert v2["version"] == 2 and v2["created_version"] is True
    same = client.put(f"/api/datasets/{ds['id']}", json={"name": "Handbook QA", "cases": CASES}).json()
    assert same["version"] == 2 and same["created_version"] is False
    bad = client.put(f"/api/datasets/{ds['id']}", json={"name": "x", "cases": [{"id": "a"}, {"id": "a"}]})
    assert bad.status_code == 422
    assert [v["version"] for v in client.get(f"/api/datasets/{ds['id']}/versions").json()] == [1, 2]
    assert {e["name"] for e in client.get("/api/evaluators").json()} >= {"recall_at_k", "correctness_judge"}

    config = {
        "evaluators": ["recall_at_k", "mrr", "contains_required", "citation_validity", "success", "latency_ms"],
        "k": 5,
    }
    ev_a = client.post(
        f"/api/projects/{pid}/evaluations", json={"dataset_id": ds["id"], "label": "top 5", "config": config}
    )
    assert ev_a.status_code == 202
    a = wait_eval(client, ev_a.json()["id"])
    assert a["status"] == "succeeded", a["error"]
    assert a["done"] == a["total"] == 3 and a["dataset_version"] == 2 and a["revision"] == rev_a
    summary = {m["name"]: m for m in a["summary"]}
    assert summary["success"]["mean"] == 1.0 and summary["recall_at_k"]["n"] == 3

    # every case links to a normal, traced run
    cases = client.get(f"/api/evaluations/{a['id']}/cases").json()
    assert [c["case_id"] for c in cases] == ["leave", "hotel", "phishing"]
    run = client.get(f"/api/runs/{cases[0]['run_id']}").json()
    assert run["status"] == "succeeded" and run["inputs"] == CASES[0]["inputs"]
    assert client.get(f"/api/runs/{cases[0]['run_id']}/events").json()[0]["event_type"] == "run_started"

    # second version of the graph: retrieve only 1 chunk
    nodes["retrieve"]["config"]["top_k"] = 1
    rev_b = client.put(f"/api/projects/{pid}", json={"document": doc}).json()["revision"]
    b = wait_eval(
        client,
        client.post(
            f"/api/projects/{pid}/evaluations", json={"dataset_id": ds["id"], "label": "top 1", "config": config}
        ).json()["id"],
    )
    assert b["status"] == "succeeded" and b["revision"] == rev_b
    diff = client.get(f"/api/evaluations/compare?a={a['id']}&b={b['id']}").json()
    assert diff["comparable"] is True and len(diff["cases"]) == 3
    assert {m["name"] for m in diff["metrics"]} >= {"recall_at_k", "mrr"}

    # baseline, and comparing across dataset versions is flagged
    assert client.put(f"/api/evaluations/{a['id']}/baseline").json()["baseline"] is True
    old = wait_eval(
        client,
        client.post(
            f"/api/projects/{pid}/evaluations",
            json={"dataset_id": ds["id"], "dataset_version": 1, "revision": rev_a, "config": config},
        ).json()["id"],
    )
    flagged = client.get(f"/api/evaluations/compare?a={old['id']}&b={a['id']}").json()
    assert flagged["comparable"] is False and flagged["only_in_b"] == ["phishing"]
    listed = client.get(f"/api/projects/{pid}/evaluations").json()
    assert sum(1 for e in listed if e["baseline"]) == 1 and len(listed) == 3

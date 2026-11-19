"""Node packages API (ADR-0021): install → pending (refused) → approve → runs isolated → disable → remove."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_node_sdk import cli as sdk_cli
from agenticmind_plugins.testing import build_wheel
from fastapi.testclient import TestClient

PROBE = Path(__file__).resolve().parents[3] / "packages" / "plugins" / "tests" / "fixtures" / "probe-nodes"


@pytest.fixture(scope="module")
def wheels(tmp_path_factory):
    root = tmp_path_factory.mktemp("wheels")
    pkg = root / "acme-text-nodes"
    assert sdk_cli.main(["new", str(pkg), "--namespace", "acme"]) == 0
    v1 = build_wheel(pkg, root / "dist1")
    pyproject = pkg / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('version = "0.1.0"', 'version = "0.2.0"'))
    v2 = build_wheel(pkg, root / "dist2")
    probe = build_wheel(PROBE, root / "dist3")
    return {"v1": v1, "v2": v2, "probe": probe}


@pytest.fixture
def make_client(tmp_path):
    def make():
        settings = Settings(database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}", data_dir=tmp_path / "data")
        return TestClient(create_app(settings))

    return make


def upload(client, wheel: Path, trust="community"):
    with wheel.open("rb") as f:
        return client.post("/api/plugins", files={"file": (wheel.name, f, "application/octet-stream")},
                           data={"trust": trust})  # fmt: skip


GRAPH = {
    "nodes": [
        {"id": "q", "type": "agenticmind.core.manual_input", "version": "1.0.0", "config": {"key": "text"}},
        {"id": "n", "type": "acme.text.word_stats", "version": "1.0.0", "config": {"top_n": 1}},
        {"id": "o", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "summary"}},
    ],
    "edges": [
        {"id": "e1", "kind": "data", "source": {"node": "q", "port": "value"},
         "target": {"node": "n", "port": "text"}},
        {"id": "e2", "kind": "data", "source": {"node": "n", "port": "summary"},
         "target": {"node": "o", "port": "value"}},
    ],
}  # fmt: skip


def make_project(client):
    pid = client.post("/api/projects", json={"name": "Uses a plugin", "template": "hello-pipeline"}).json()["id"]
    doc = client.get(f"/api/projects/{pid}").json()["document"]
    doc["nodes"], doc["edges"] = GRAPH["nodes"], GRAPH["edges"]
    client.put(f"/api/projects/{pid}", json={"document": doc})
    return pid


def run(client, pid, text):
    rid = client.post(f"/api/projects/{pid}/runs", json={"inputs": {"text": text}}).json()["id"]
    for _ in range(300):
        r = client.get(f"/api/runs/{rid}").json()
        if r["status"] in ("succeeded", "failed"):
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_install_review_approve_run_disable_remove(make_client, wheels):
    with make_client() as c:
        pkg = upload(c, wheels["v1"]).json()
        assert pkg["name"] == "acme-text-nodes" and pkg["status"] == "pending" and pkg["requested"] == []
        assert pkg["nodes"][0]["type"] == "acme.text.word_stats"
        node = next(n for n in c.get("/api/nodes").json() if n["type"] == "acme.text.word_stats")
        assert node["isolated"] and not node["permitted"] and node["trust"] == "community"

        pid = make_project(c)
        report = c.post(f"/api/projects/{pid}/validate").json()
        assert "NODE_NOT_PERMITTED" in [d["code"] for d in report["diagnostics"]]

        assert c.post("/api/plugins/acme-text-nodes/approve", json={"sha256": pkg["sha256"]}).status_code == 400
        wrong = c.post("/api/plugins/acme-text-nodes/approve", json={"sha256": "x" * 64, "confirm": True})
        assert wrong.status_code == 409
        body = {"sha256": pkg["sha256"], "confirm": True}
        ok = c.post("/api/plugins/acme-text-nodes/approve", json=body).json()
        assert ok["status"] == "approved"

        r = run(c, pid, "Graphs run. Graphs evaluate. Graphs deploy!")
        assert r["status"] == "succeeded", r["error"]
        assert r["outputs"]["summary"] == "6 words in 3 sentence(s); most frequent: graphs"

        export = c.get(f"/api/projects/{pid}/app/preview")
        assert export.status_code == 422 and "plugins" in export.json()["detail"]["message"]
        plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "docker_server"})
        assert plan.status_code == 422  # deploying builds on the export, so it says the same
        assert c.post("/api/plugins/acme-text-nodes/disable").json()["status"] == "disabled"
        assert not any(n["type"] == "acme.text.word_stats" for n in c.get("/api/nodes").json())
        assert c.post("/api/plugins/acme-text-nodes/enable").json()["status"] == "approved"  # approval kept

        data = c.get("/api/plugins/acme-text-nodes/wheel")
        assert data.status_code == 200 and data.content == wheels["v1"].read_bytes()  # export what was imported

        assert c.delete("/api/plugins/acme-text-nodes").status_code == 204
        assert c.get("/api/plugins").json()["packages"] == []


def test_new_version_needs_a_new_approval_and_restart_keeps_state(make_client, wheels):
    with make_client() as c:
        v1 = upload(c, wheels["v1"]).json()
        c.post("/api/plugins/acme-text-nodes/approve", json={"sha256": v1["sha256"], "confirm": True})
        v2 = upload(c, wheels["v2"]).json()
        assert v2["version"] == "0.2.0" and v2["status"] == "pending"
    with make_client() as c:  # a fresh server process: packages come back from stored manifests
        pkgs = c.get("/api/plugins").json()["packages"]
        assert [(p["name"], p["version"], p["status"]) for p in pkgs] == [("acme-text-nodes", "0.2.0", "pending")]
        node = next(n for n in c.get("/api/nodes").json() if n["type"] == "acme.text.word_stats")
        assert node["isolated"] and not node["permitted"]


def test_permissions_are_shown_and_must_match_exactly(make_client, wheels):
    with make_client() as c:
        pkg = upload(c, wheels["probe"], trust="private").json()
        assert pkg["trust"] == "private" and pkg["requested"] == ["credentials", "filesystem"]
        fewer = c.post("/api/plugins/probe-nodes/approve",
                       json={"sha256": pkg["sha256"], "permissions": ["filesystem"], "confirm": True})  # fmt: skip
        assert fewer.status_code == 422 and "exactly" in fewer.json()["detail"]
        both = {"sha256": pkg["sha256"], "permissions": ["filesystem", "credentials"], "confirm": True}
        ok = c.post("/api/plugins/probe-nodes/approve", json=both)
        assert ok.json()["granted"] == ["credentials", "filesystem"]


def test_bad_uploads_are_rejected(make_client, tmp_path):
    with make_client() as c:
        junk = tmp_path / "junk.whl"
        junk.write_bytes(b"not a zip")
        r = upload(c, junk)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "PLUGIN_NOT_A_WHEEL"
        assert c.post("/api/plugins/nope/approve", json={"sha256": "x", "confirm": True}).status_code == 404

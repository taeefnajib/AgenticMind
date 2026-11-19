"""Deployment Center API (ADR-0020): plan → confirm (hash-bound) → deploy → health → supersede → rollback.

Docker itself is faked here (``LocalDocker.up/down``); the live check runs real Docker."""

from __future__ import annotations

import io
import time
import zipfile

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_deployment import local as local_mod
from agenticmind_runtime import MappingSecretResolver
from fastapi.testclient import TestClient

SECRETS = {"OPENROUTER_API_KEY": "fixture-secret-value-123", "APP_API_KEYS": "fixture-client-key"}


@pytest.fixture
def make_client(tmp_path):
    def make(local: bool = True, secrets=SECRETS):
        settings = Settings(
            database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            data_dir=tmp_path / "data",
            local_deploy=local,
        )
        return TestClient(create_app(settings, secrets=MappingSecretResolver(secrets)))

    return make


@pytest.fixture
def fake_docker(monkeypatch):
    calls = {"up": [], "down": [], "env": []}
    monkeypatch.setattr("agenticmind_api.deploy_manager.docker_status", lambda: (True, "Docker fake"))

    def up(self, workdir, project, *, secrets, port, build_timeout=1800):
        calls["up"].append(project)
        calls["env"].append(dict(secrets))
        assert (workdir / "deploy/local/compose.yaml").exists() and (workdir / "Dockerfile").exists()
        return local_mod.LocalResult(True, f"http://127.0.0.1:{port}")

    def down(self, workdir, project, *, timeout=180):
        calls["down"].append(project)
        return True

    monkeypatch.setattr(local_mod.LocalDocker, "up", up)
    monkeypatch.setattr(local_mod.LocalDocker, "down", down)
    monkeypatch.setattr("agenticmind_api.deploy_manager.health", lambda url, **kw: True)
    return calls


def project(client, template="ticket-triage"):
    return client.post("/api/projects", json={"name": "Deploy me", "template": template}).json()["id"]


def wait(client, dep_id, statuses=("running", "failed"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        dep = client.get(f"/api/deployments/{dep_id}").json()
        if dep["status"] in statuses:
            return dep
        time.sleep(0.05)
    raise AssertionError(f"deployment stayed {dep['status']}")


def test_targets_and_local_switch(make_client):
    with make_client(local=False) as c:
        body = c.get("/api/deployment/targets").json()
        assert {t["id"] for t in body["targets"]} >= {"local", "kubernetes", "cloud_run", "vercel"}
        assert body["local"]["available"] is False and "AGENTICMIND_LOCAL_DEPLOY" in body["local"]["reason"]
        pid = project(c)
        plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "local"}).json()
        assert not plan["compatible"] and plan["checks"][0]["id"] == "local-docker"


def test_confirmation_is_required_and_bound_to_the_plan(make_client, fake_docker):
    with make_client() as c:
        pid = project(c)
        plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "kubernetes"}).json()
        assert plan["compatible"] and plan["files"]["deploy/kubernetes/kustomization.yaml"]
        req = {"target": "kubernetes", "plan_hash": plan["plan_hash"]}
        assert c.post(f"/api/projects/{pid}/deployments", json=req).status_code == 400  # no confirm
        changed = c.post(f"/api/projects/{pid}/deployments", json={**req, "confirm": True, "profile": "enterprise"})
        assert changed.status_code == 409 and "changed" in changed.json()["detail"]["message"]
        blocked_plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "vercel"}).json()
        assert blocked_plan["compatible"]  # ticket-triage has no local models or disk: only warnings
        dep = c.post(f"/api/projects/{pid}/deployments", json={**req, "confirm": True}).json()
        assert dep["status"] == "artifacts_ready" and dep["mode"] == "guided"
        zipped = c.get(f"/api/deployments/{dep['id']}/bundle")
        names = zipfile.ZipFile(io.BytesIO(zipped.content)).namelist()
        assert any(n.endswith("deploy/kubernetes/deployment.yaml") for n in names)
        assert b"fixture-secret-value-123" not in zipped.content
        live = c.post(f"/api/deployments/{dep['id']}/url", json={"url": "https://triage.example.org"}).json()
        assert live["status"] == "live" and live["health"]["ok"]


def test_blockers_refuse_deployment(make_client, fake_docker):
    with make_client() as c:
        pid = project(c, "advanced-rag")
        plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "cloud_run"}).json()
        assert not plan["compatible"]
        r = c.post(
            f"/api/projects/{pid}/deployments",
            json={"target": "cloud_run", "plan_hash": plan["plan_hash"], "confirm": True},
        )
        assert r.status_code == 422 and r.json()["detail"]["blockers"][0]["id"] == "persistent-data"


def test_local_deploy_switch_over_and_rollback(make_client, fake_docker):
    with make_client() as c:
        pid = project(c)
        plan1 = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "local"}).json()
        assert plan1["compatible"], plan1["checks"]
        assert {s["name"]: s["provided"] for s in plan1["secrets"]} == {
            "APP_API_KEYS": True,
            "OPENROUTER_API_KEY": True,
        }
        d1 = c.post(
            f"/api/projects/{pid}/deployments",
            json={"target": "local", "plan_hash": plan1["plan_hash"], "confirm": True},
        ).json()
        d1 = wait(c, d1["id"])
        assert d1["status"] == "running" and d1["endpoint"].startswith("http://127.0.0.1:81")
        assert fake_docker["env"][0] == SECRETS  # values went to the subprocess environment only
        row_text = str(c.get(f"/api/deployments/{d1['id']}").json())
        assert "fixture-secret-value-123" not in row_text and "fixture-client-key" not in row_text

        # save a new revision and deploy it: the old one is stopped only after the new one is healthy
        doc = c.get(f"/api/projects/{pid}").json()["document"]
        doc["project"]["description"] = "v2"
        c.put(f"/api/projects/{pid}", json={"document": doc})
        plan2 = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "local"}).json()
        assert plan2["revision"] == d1["revision"] + 1
        d2 = c.post(
            f"/api/projects/{pid}/deployments",
            json={"target": "local", "plan_hash": plan2["plan_hash"], "confirm": True},
        ).json()
        d2 = wait(c, d2["id"])
        assert d2["status"] == "running" and d2["endpoint"] != d1["endpoint"]
        history = {d["id"]: d for d in c.get(f"/api/projects/{pid}/deployments").json()}
        assert history[d1["id"]]["status"] == "superseded"
        assert history[d1["id"]]["health"] is None  # no stale "healthy" on a stopped deployment
        assert fake_docker["down"] == [fake_docker["up"][0]]

        # roll back to revision 1: review its plan, confirm, and it supersedes revision 2
        plan_back = c.post(
            f"/api/projects/{pid}/deployments/plan", json={"target": "local", "revision": d1["revision"]}
        ).json()
        d3 = c.post(
            f"/api/projects/{pid}/deployments",
            json={"target": "local", "revision": d1["revision"], "plan_hash": plan_back["plan_hash"],
                  "confirm": True, "rollback_of": d1["id"]},
        ).json()  # fmt: skip
        d3 = wait(c, d3["id"])
        assert d3["revision"] == d1["revision"] and d3["rollback_of"] == d1["id"] and d3["status"] == "running"
        assert c.get(f"/api/deployments/{d2['id']}").json()["status"] == "superseded"
        stopped = c.post(f"/api/deployments/{d3['id']}/stop").json()
        assert stopped["status"] == "stopped"


def test_local_deploy_needs_secrets_on_the_server(make_client, fake_docker):
    with make_client(secrets={"OPENROUTER_API_KEY": "x"}) as c:
        pid = project(c)
        plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "local"}).json()
        blocker = next(ch for ch in plan["checks"] if ch["id"] == "secrets-missing")
        assert "APP_API_KEYS" in blocker["detail"]


def test_failed_local_deploy_is_recorded_and_cleaned_up(make_client, fake_docker, monkeypatch):
    def broken(self, workdir, project, *, secrets, port, build_timeout=1800):
        self.log("pip install failed")
        return local_mod.LocalResult(False, error="docker compose up failed (exit 1); see the deployment log")

    monkeypatch.setattr(local_mod.LocalDocker, "up", broken)
    with make_client() as c:
        pid = project(c)
        plan = c.post(f"/api/projects/{pid}/deployments/plan", json={"target": "local"}).json()
        dep = c.post(
            f"/api/projects/{pid}/deployments",
            json={"target": "local", "plan_hash": plan["plan_hash"], "confirm": True},
        ).json()
        dep = wait(c, dep["id"])
        assert dep["status"] == "failed" and "exit 1" in dep["error"] and "pip install failed" in dep["log"]
        assert fake_docker["down"]  # the half-started project was taken down

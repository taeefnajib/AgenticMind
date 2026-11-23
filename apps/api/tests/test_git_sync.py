"""GitHub version control through the API, against the in-memory fake GitHub."""

from __future__ import annotations

import json
import time

import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_git.testing import FakeGitHub
from agenticmind_runtime.secrets import MappingSecretResolver
from fastapi.testclient import TestClient


def graph(name: str = "Support bot", default: str = "hi") -> dict:
    return {
        "ir_version": "1.0",
        "project": {"id": "template", "name": name},
        "nodes": [
            {
                "id": "q",
                "type": "agenticmind.core.manual_input",
                "version": "1.0.0",
                "config": {"key": "q", "value_type": "String", "default": default},
            }
        ],
        "edges": [],
        "metadata": {},
    }


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode="off",
        mcp_server=False,
    )
    app = create_app(settings, secrets=MappingSecretResolver({"GITHUB_TOKEN": "fixture-gh-token"}))
    fake = FakeGitHub("acme/agents")
    with TestClient(app) as client:
        app.state.services.git.transport = fake.transport
        pid = client.post("/api/projects", json={"name": "Support bot", "document": graph()}).json()["id"]
        yield client, fake, pid


def save(client, pid, doc):
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    r = client.put(f"/api/projects/{pid}", json={"document": doc, "expected_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def test_link_push_pull_history_restore(env):
    client, fake, pid = env
    bad = client.put(f"/api/projects/{pid}/git", json={"repo": "not a repo"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "repo"
    link = client.put(f"/api/projects/{pid}/git", json={"repo": "acme/agents", "folder": "bots/support"}).json()
    assert link["file"] == "bots/support/project.agenticmind.json" and link["token_configured"] is True
    assert client.post(f"/api/projects/{pid}/git/check", json={"repo": "acme/agents"}).json()["can_push"] is True

    first = client.post(f"/api/projects/{pid}/git/push", json={"message": "First version"}).json()
    assert first["status"] == "pushed" and first["files"] == 2
    ir = json.loads(fake.file("main", "bots/support/project.agenticmind.json"))
    assert ir["project"]["id"] == pid and fake.file("main", "README.md")  # existing files kept
    assert client.post(f"/api/projects/{pid}/git/push", json={}).json()["status"] == "up_to_date"

    doc = graph(default="hello")
    doc["project"]["id"] = pid
    save(client, pid, doc)
    assert client.post(f"/api/projects/{pid}/git/push", json={}).json()["status"] == "pushed"
    history = client.get(f"/api/projects/{pid}/git/history").json()
    assert [h["message"] for h in history][-1] == "First version" and len(history) == 2

    # someone edits the file on GitHub: pushing now would overwrite their change
    theirs = dict(ir, nodes=[dict(ir["nodes"][0], config={"key": "q", "value_type": "String", "default": "remote"})])
    fake.edit_remote("main", "bots/support/project.agenticmind.json", json.dumps(theirs).encode())
    doc["nodes"][0]["config"]["default"] = "local change"
    save(client, pid, doc)
    blocked = client.post(f"/api/projects/{pid}/git/push", json={})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "GIT_REMOTE_CHANGED"
    pulled = client.post(f"/api/projects/{pid}/git/pull").json()
    assert pulled["status"] == "pulled"
    current = client.get(f"/api/projects/{pid}").json()["document"]
    assert current["nodes"][0]["config"]["default"] == "remote" and current["project"]["id"] == pid
    assert client.post(f"/api/projects/{pid}/git/push", json={}).json()["status"] == "up_to_date"

    oldest = client.get(f"/api/projects/{pid}/git/history").json()[-1]["sha"]
    restored = client.post(f"/api/projects/{pid}/git/restore", json={"sha": oldest}).json()
    assert restored["status"] == "pulled"
    assert client.get(f"/api/projects/{pid}").json()["document"]["nodes"][0]["config"]["default"] == "hi"
    assert client.post(f"/api/projects/{pid}/git/restore", json={"sha": "zz"}).status_code == 422


def test_auto_push_force_and_missing_token(env):
    client, fake, pid = env
    client.put(f"/api/projects/{pid}/git", json={"repo": "acme/agents", "auto_push": True, "branch": "agents"})
    doc = graph(default="auto")
    doc["project"]["id"] = pid
    save(client, pid, doc)
    for _ in range(100):
        if fake.file("agents", "project.agenticmind.json"):
            break
        time.sleep(0.05)
    assert json.loads(fake.file("agents", "project.agenticmind.json"))["nodes"][0]["config"]["default"] == "auto"
    state = client.get(f"/api/projects/{pid}/git").json()["state"]
    assert state["commit"] and state.get("error") is None
    fake.edit_remote("agents", "project.agenticmind.json", b'{"edited": true}')
    doc["nodes"][0]["config"]["default"] = "mine"
    save(client, pid, doc)
    time.sleep(0.5)
    assert "auto-push" in client.get(f"/api/projects/{pid}/git").json()["state"]["error"]  # refused, recorded
    forced = client.post(f"/api/projects/{pid}/git/push", json={"force": True}).json()
    assert forced["status"] == "pushed"
    assert json.loads(fake.file("agents", "project.agenticmind.json"))["nodes"][0]["config"]["default"] == "mine"
    client.put(f"/api/projects/{pid}/git", json={"repo": "acme/agents", "token_secret": "NOT_SET"})
    missing = client.post(f"/api/projects/{pid}/git/push", json={})
    assert missing.status_code == 400 and missing.json()["detail"]["code"] == "GIT_TOKEN_MISSING"
    assert client.delete(f"/api/projects/{pid}/git").json()["link"] is None


def test_create_repo_when_missing(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode="off",
        mcp_server=False,
    )
    app = create_app(settings, secrets=MappingSecretResolver({"GITHUB_TOKEN": "fixture-gh-token"}))
    fake = FakeGitHub("acme/new-bot", exists=False)
    with TestClient(app) as client:
        app.state.services.git.transport = fake.transport
        pid = client.post("/api/projects", json={"name": "New bot", "document": graph("New bot")}).json()["id"]
        client.put(f"/api/projects/{pid}/git", json={"repo": "acme/new-bot", "create_repo": True, "private": True})
        assert client.post(f"/api/projects/{pid}/git/push", json={}).json()["status"] == "pushed"
    assert fake.created_repo["private"] is True and fake.created_repo["auto_init"] is True
    assert fake.file("main", "project.agenticmind.json")

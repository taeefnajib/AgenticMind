"""Workspace secrets (ADR-0026): encrypted at rest, write-only, environment first, admin-only edits."""

from __future__ import annotations

import base64

from agenticmind_api.db import SecretRow
from agenticmind_api.main import create_app
from agenticmind_api.secrets_store import load_key
from agenticmind_api.settings import Settings
from agenticmind_runtime import MappingSecretResolver
from fastapi.testclient import TestClient
from sqlalchemy import select

ADMIN = {"username": "ada", "display_name": "Ada", "password": "correct horse battery"}


def make(tmp_path, env=None, auth="required"):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}", data_dir=tmp_path / "data", auth_mode=auth
    )
    return create_app(settings, secrets=MappingSecretResolver(env or {}))


def test_secrets_are_encrypted_write_only_and_resolved(tmp_path):
    app = make(tmp_path, env={"FROM_ENV": "env-value", "BOTH": "env-wins"})
    admin = TestClient(app)
    admin.post("/api/auth/setup", json=ADMIN)
    r = admin.put("/api/secrets/WAREHOUSE_TOKEN", json={"value": "dapi-fixture-value"})
    assert r.status_code == 200 and r.json()["name"] == "WAREHOUSE_TOKEN" and "value" not in r.json()
    admin.put("/api/secrets/BOTH", json={"value": "stored"})
    listed = admin.get("/api/secrets").json()
    assert [s["name"] for s in listed["secrets"]] == ["BOTH", "WAREHOUSE_TOKEN"] and listed["can_edit"]
    assert listed["secrets"][0]["overridden_by_environment"] is True
    assert "dapi-fixture-value" not in admin.get("/api/secrets").text
    status = admin.post("/api/secrets/status", json={"names": ["WAREHOUSE_TOKEN", "FROM_ENV", "MISSING"]}).json()
    assert status == {"WAREHOUSE_TOKEN": "workspace", "FROM_ENV": "environment", "MISSING": None}
    chain = app.state.services.secrets
    assert chain.get("WAREHOUSE_TOKEN") == "dapi-fixture-value" and chain.get("BOTH") == "env-wins"
    # at rest: ciphertext only, bound to its name
    sf = app.state.services.auth._sf
    with sf() as s:
        stored = s.scalar(select(SecretRow.ciphertext).where(SecretRow.name == "WAREHOUSE_TOKEN"))
    assert "dapi-fixture-value" not in stored and "dapi" not in base64.b64decode(stored).decode("latin-1")
    with sf.begin() as s:  # a ciphertext moved to another name does not decrypt
        s.add(SecretRow(name="MOVED", ciphertext=stored))
    assert chain.get("MOVED") is None
    # the assistant settings see workspace secrets too
    admin.put(
        "/api/settings/assistant", json={"provider": "openrouter", "model": "a/b", "api_key_secret": "WAREHOUSE_TOKEN"}
    )
    assert admin.get("/api/settings/assistant").json()["configured"] is True
    assert admin.delete("/api/secrets/WAREHOUSE_TOKEN").status_code == 204
    assert admin.delete("/api/secrets/WAREHOUSE_TOKEN").status_code == 404
    bad = admin.put("/api/secrets/1BAD", json={"value": "x"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "SECRET_NAME_INVALID"


def test_members_see_names_but_cannot_edit(tmp_path):
    app = make(tmp_path)
    admin = TestClient(app)
    admin.post("/api/auth/setup", json=ADMIN)
    admin.put("/api/secrets/DB_PASSWORD", json={"value": "pw"})
    admin.post("/api/users", json={"username": "bob", "display_name": "Bob", "password": "first password"})
    bob = TestClient(app)
    bob.post("/api/auth/login", json={"username": "bob", "password": "first password"})
    assert bob.get("/api/secrets").json()["can_edit"] is False
    assert bob.put("/api/secrets/DB_PASSWORD", json={"value": "mine"}).status_code == 403
    assert bob.delete("/api/secrets/DB_PASSWORD").status_code == 403
    assert bob.post("/api/secrets/status", json={"names": ["DB_PASSWORD"]}).json() == {"DB_PASSWORD": "workspace"}


def test_key_file_is_created_once_and_env_key_wins(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTICMIND_SECRET_KEY", raising=False)
    first = load_key(tmp_path)
    assert (tmp_path / "secret.key").exists() and load_key(tmp_path) == first and len(first) == 32
    monkeypatch.setenv("AGENTICMIND_SECRET_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    assert load_key(tmp_path) == b"k" * 32

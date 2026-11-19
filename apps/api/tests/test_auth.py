"""Sign-in, profiles, user management and workspace settings (ADR-0023)."""

from __future__ import annotations

import json

import pytest
from agenticmind_api.auth import SESSION_COOKIE, hash_password, verify_password
from agenticmind_api.db import SessionRow
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_node_sdk import ModelUsage
from agenticmind_providers import ChatResponse, register_provider
from agenticmind_runtime import MappingSecretResolver
from fastapi.testclient import TestClient
from sqlalchemy import select

ADMIN = {"username": "ada", "display_name": "Ada Admin", "password": "correct horse battery"}


def make_app(tmp_path, *, auth="required", secrets=None):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}",
        data_dir=tmp_path / "data",
        auth_mode=auth,
        assistant_provider="mock",
        assistant_model="mock-1",
    )
    return create_app(settings, secrets=MappingSecretResolver(secrets or {}))


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


def signed_in(app, creds=ADMIN, *, setup=False) -> TestClient:
    client = TestClient(app)
    if setup:
        r = client.post("/api/auth/setup", json=creds)
    else:
        r = client.post("/api/auth/login", json={"username": creds["username"], "password": creds["password"]})
    assert r.status_code in (200, 201), r.text
    return client


def test_first_run_setup_then_everything_needs_a_session(app):
    anon = TestClient(app)
    assert anon.get("/api/health").status_code == 200  # public
    assert anon.get("/api/auth/status").json() == {"mode": "required", "setup_required": True, "user": None}
    r = anon.get("/api/projects")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "AUTH_REQUIRED"

    admin = signed_in(app, setup=True)
    status = admin.get("/api/auth/status").json()
    assert status["setup_required"] is False and status["user"]["role"] == "admin"
    assert admin.get("/api/projects").status_code == 200
    assert admin.get("/api/auth/me").json()["sessions"] == 1
    # only the very first account comes from setup
    again = TestClient(app).post("/api/auth/setup", json={**ADMIN, "username": "eve"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "SETUP_DONE"
    assert anon.get("/api/projects").status_code == 401


def test_cookie_is_httponly_and_only_a_hash_is_stored(app):
    client = TestClient(app)
    r = client.post("/api/auth/setup", json=ADMIN)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=") and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    token = client.cookies[SESSION_COOKIE]
    sf = app.state.services.auth._sf
    with sf() as s:
        hashes = list(s.scalars(select(SessionRow.token_hash)))
    assert len(hashes) == 1 and token not in hashes[0]
    # and passwords are salted scrypt hashes
    stored = app.state.services.auth.list_users()[0].password_hash
    assert stored.startswith("scrypt$") and ADMIN["password"] not in stored
    assert verify_password(ADMIN["password"], stored) and not verify_password("nope", stored)
    assert hash_password("same password") != hash_password("same password")


def test_login_logout_and_rate_limit(app):
    signed_in(app, setup=True)
    client = TestClient(app)
    bad = client.post("/api/auth/login", json={"username": "ada", "password": "wrong password"})
    assert bad.status_code == 401 and bad.json()["detail"]["code"] == "LOGIN_FAILED"
    unknown = client.post("/api/auth/login", json={"username": "nobody", "password": "wrong password"})
    assert unknown.json()["detail"]["message"] == bad.json()["detail"]["message"]  # no username probing
    ok = client.post("/api/auth/login", json={"username": " ADA ", "password": ADMIN["password"]})
    assert ok.status_code == 200 and ok.json()["username"] == "ada"
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/projects").status_code == 401
    # five failures lock the username for a while, even with the right password
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "ada", "password": "wrong password"})
    locked = client.post("/api/auth/login", json={"username": "ada", "password": ADMIN["password"]})
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0


def test_profile_and_password_change_signs_out_other_browsers(app):
    laptop = signed_in(app, setup=True)
    phone = signed_in(app)
    r = laptop.patch("/api/auth/me", json={"display_name": "  Ada   Lovelace ", "email": "ada@example.test"})
    assert r.json()["display_name"] == "Ada Lovelace" and r.json()["email"] == "ada@example.test"
    assert laptop.patch("/api/auth/me", json={"email": "not-an-email"}).status_code == 422
    wrong = laptop.post("/api/auth/me/password", json={"current_password": "x" * 8, "new_password": "y" * 12})
    assert wrong.status_code == 403
    short = laptop.post("/api/auth/me/password", json={"current_password": ADMIN["password"], "new_password": "short"})
    assert short.status_code == 422 and short.json()["detail"]["code"] == "PASSWORD_TOO_SHORT"
    new = "a much better passphrase"
    ok = laptop.post("/api/auth/me/password", json={"current_password": ADMIN["password"], "new_password": new})
    assert ok.status_code == 204
    assert laptop.get("/api/projects").status_code == 200  # this browser stays signed in
    assert phone.get("/api/projects").status_code == 401  # the other one is signed out
    assert TestClient(app).post("/api/auth/login", json={"username": "ada", "password": new}).status_code == 200


def test_admins_manage_people_and_members_cannot(app):
    admin = signed_in(app, setup=True)
    r = admin.post(
        "/api/users", json={"username": "bob", "display_name": "Bob", "password": "first password", "role": "member"}
    )
    assert r.status_code == 201
    bob_id = r.json()["id"]
    taken = admin.post("/api/users", json={"username": "BOB", "display_name": "B", "password": "first password"})
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "USERNAME_TAKEN"
    assert admin.post("/api/users", json={"username": "x", "display_name": "X", "password": "p" * 9}).status_code == 422

    bob = signed_in(app, {"username": "bob", "password": "first password"})
    assert bob.get("/api/projects").status_code == 200  # members build and run workflows
    for method, path, kw in [
        ("get", "/api/users", {}),
        ("patch", f"/api/users/{bob_id}", {"json": {"role": "admin"}}),
        ("put", "/api/settings/assistant", {"json": {"provider": "mock", "model": "mock-1"}}),
        ("post", "/api/plugins/x/approve", {"json": {"sha256": "0", "permissions": []}}),
        ("delete", "/api/plugins/x", {}),
    ]:
        r = getattr(bob, method)(path, **kw)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "ADMIN_REQUIRED", (path, r.text)
    assert bob.get("/api/settings/assistant").json()["can_edit"] is False

    # deactivating signs the person out; the last admin can't be demoted or deactivate themselves
    assert admin.patch(f"/api/users/{bob_id}", json={"active": False}).json()["active"] is False
    assert bob.get("/api/projects").status_code == 401
    me = admin.get("/api/auth/me").json()["id"]
    demote = admin.patch(f"/api/users/{me}", json={"role": "member"})
    assert demote.status_code == 409 and demote.json()["detail"]["code"] == "LAST_ADMIN"
    assert admin.patch(f"/api/users/{me}", json={"active": False}).json()["detail"]["code"] == "SELF_DEACTIVATE"
    # a reset password lets them back in once reactivated
    admin.patch(f"/api/users/{bob_id}", json={"active": True, "password": "reset password"})
    assert TestClient(app).post("/api/auth/login", json={"username": "bob", "password": "reset password"}).is_success


def test_cross_site_writes_are_refused(app):
    admin = signed_in(app, setup=True)
    evil = admin.post("/api/projects", json={"name": "x"}, headers={"Origin": "http://localhost:9999"})
    assert evil.status_code == 403 and evil.json()["detail"]["code"] == "ORIGIN_REFUSED"
    ref = admin.post("/api/projects", json={"name": "x"}, headers={"Referer": "http://evil.test/page"})
    assert ref.status_code == 403
    assert admin.post("/api/projects", json={"name": "x"}, headers={"Origin": "http://localhost:5173"}).is_success
    assert admin.post("/api/projects", json={"name": "x"}, headers={"Origin": "http://testserver"}).is_success
    assert admin.get("/api/projects", headers={"Origin": "http://localhost:9999"}).is_success  # reads are fine


class RecordingProvider:
    name = "settings-test"

    def __init__(self) -> None:
        self.models: list[str] = []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.models.append(request.model)
        text = json.dumps({"reply": "Nothing to change.", "change": None})
        return ChatResponse(text=text, usage=ModelUsage(provider=self.name, model=request.model), model=request.model)


def test_settings_choose_the_assistant_model(app):
    provider = RecordingProvider()
    register_provider(provider)
    admin = signed_in(app, setup=True)
    state = admin.get("/api/settings/assistant").json()
    assert state["source"] == "environment" and state["assistant"]["model"] == "mock-1" and state["can_edit"]
    assert {"name": "openrouter", "needs_key": True} in state["providers"]

    bad = admin.put("/api/settings/assistant", json={"provider": "nope", "model": "m"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "provider"
    spaced = admin.put("/api/settings/assistant", json={"provider": "mock", "model": "has space"})
    assert spaced.json()["detail"]["field"] == "model"
    secret = admin.put(
        "/api/settings/assistant", json={"provider": "openrouter", "model": "a/b", "api_key_secret": "x"}
    )
    assert secret.json()["detail"]["field"] == "api_key_secret"

    saved = admin.put(
        "/api/settings/assistant", json={"provider": "settings-test", "model": "vendor/model-2", "max_attempts": 2}
    ).json()
    assert saved["source"] == "workspace" and saved["assistant"]["model"] == "vendor/model-2"
    assert admin.get("/api/assistant").json()["model"] == "vendor/model-2"
    project = admin.post("/api/projects", json={"name": "P"}).json()
    admin.post(f"/api/projects/{project['id']}/assistant", json={"message": "hello"})
    assert provider.models == ["vendor/model-2"]  # the designer uses the saved model

    test = admin.post("/api/settings/assistant/test", json={"provider": "mock", "model": "mock-1"}).json()
    assert test["ok"] is True and test["latency_ms"] >= 0
    missing = admin.post(
        "/api/settings/assistant/test",
        json={"provider": "openrouter", "model": "a/b", "api_key_secret": "ABSENT_KEY"},
    ).json()
    assert missing == {"ok": False, "code": "SECRET_NOT_CONFIGURED", "message": "The secret ABSENT_KEY is not set."}
    models = admin.post("/api/settings/assistant/models", json={"provider": "mock", "model": "mock-1"}).json()
    assert models["models"][0]["id"] == "mock-1"

    reset = admin.delete("/api/settings/assistant").json()
    assert reset["source"] == "environment" and reset["assistant"]["model"] == "mock-1"


def test_sign_in_off_keeps_single_user_mode(tmp_path):
    client = TestClient(make_app(tmp_path, auth="off"))
    assert client.get("/api/auth/status").json() == {"mode": "off", "setup_required": False, "user": None}
    assert client.get("/api/projects").status_code == 200
    assert client.get("/api/auth/me").json()["detail"]["code"] == "AUTH_OFF"
    assert client.get("/api/settings/assistant").json()["can_edit"] is True
    with pytest.raises(ValueError, match="AGENTICMIND_AUTH"):
        make_app(tmp_path / "x", auth="maybe")

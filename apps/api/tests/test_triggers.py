"""Triggers end to end: the service activates trigger nodes, verifies callers, starts recorded runs and replies.

Outbound replies go to a mock HTTP transport; mailboxes and queues are fakes. Runs are real.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta

import httpx
import pytest
from agenticmind_api.main import create_app
from agenticmind_api.settings import Settings
from agenticmind_api.triggers import channels as ch
from agenticmind_runtime.secrets import MappingSecretResolver
from fastapi.testclient import TestClient

SECRETS = {
    "HOOK_TOKEN": "fixture-hook-token",
    "SLACK_SIGNING": "fixture-slack-signing",
    "SLACK_BOT": "fixture-slack-bot",
    "TG_BOT": "fixture-telegram-bot",
    "TG_SECRET": "fixture-telegram-header",
    "WA_VERIFY": "fixture-wa-verify",
    "WA_APP": "fixture-wa-app-secret",
    "WA_TOKEN": "fixture-wa-access",
    "MAIL_PW": "fixture-mail-password",
}


def ref(name: str) -> dict:
    return {"$secret": name}


def graph(trigger_type: str, config: dict, *, template: str = "You said: {{ text }}") -> dict:
    return {
        "ir_version": "1.0",
        "project": {"id": "template", "name": "Trigger test"},
        "nodes": [
            {"id": "trigger", "type": trigger_type, "version": "1.0.0", "config": config},
            {"id": "reply", "type": "agenticmind.core.text_template", "version": "1.0.0",
             "config": {"template": template, "strict": True}},
            {"id": "answer", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "answer"}},
            {"id": "payload", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "payload"}},
        ],
        "edges": [
            {"id": "e1", "kind": "data", "source": {"node": "trigger", "port": "text"},
             "target": {"node": "reply", "port": "text"}},
            {"id": "e2", "kind": "data", "source": {"node": "reply", "port": "text"},
             "target": {"node": "answer", "port": "value"}},
            {"id": "e3", "kind": "data", "source": {"node": "trigger", "port": "payload"},
             "target": {"node": "payload", "port": "value"}},
        ],
        "metadata": {},
    }  # fmt: skip


class Outbox:
    """Mock transport recording outbound requests (replies)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "slack.com" in request.url.host:
            return httpx.Response(200, json={"ok": True, "ts": "1.2"})
        return httpx.Response(200, json={"ok": True})

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def env(tmp_path):
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 't.db').as_posix()}", data_dir=tmp_path / "data",
                        auth_mode="off", mcp_server=False)  # fmt: skip
    app = create_app(settings, secrets=MappingSecretResolver(SECRETS))
    outbox = Outbox()
    with TestClient(app) as client:
        svc = app.state.services
        svc.triggers.http_transport = httpx.MockTransport(outbox.handler)
        yield client, svc, outbox


def create(client, svc, trigger_type: str, config: dict, **kw) -> str:
    r = client.post("/api/projects", json={"name": "T", "document": graph(trigger_type, config, **kw)})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    client.portal.call(svc.triggers.refresh)
    return pid


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def last_run(client, pid: str) -> dict:
    return wait_for(lambda: next((r for r in client.get(f"/api/projects/{pid}/runs").json()
                                  if r["status"] in ("succeeded", "failed")), None))  # fmt: skip


# ---- webhook ------------------------------------------------------------------------------------
def test_webhook_token_and_wait_response(env):
    client, svc, _ = env
    pid = create(client, svc, "agenticmind.core.webhook_trigger",
                 {"secret": ref("HOOK_TOKEN"), "respond": "wait", "response_output": "payload"})  # fmt: skip
    [status] = client.get(f"/api/projects/{pid}/triggers").json()
    assert status["active"] and status["path"] == f"/api/triggers/{pid}/trigger"
    url = status["path"]
    assert client.post(url, json={"x": 1}).status_code == 401
    assert client.post(url, json={"x": 1}, headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.post(url, json={"order": 42}, headers={"Authorization": "Bearer fixture-hook-token"})
    assert ok.status_code == 200 and ok.json()["output"] == {"order": 42} and ok.json()["status"] == "succeeded"
    run = client.get(f"/api/runs/{ok.json()['run_id']}").json()
    assert run["inputs"]["_trigger"]["kind"] == "webhook"  # the event is part of the recorded run


def test_webhook_hmac_and_ack(env):
    client, svc, _ = env
    pid = create(client, svc, "agenticmind.core.webhook_trigger", {"auth": "hmac", "secret": ref("HOOK_TOKEN")})
    body = json.dumps({"event": "push"}).encode()
    sig = "sha256=" + hmac.new(b"fixture-hook-token", body, hashlib.sha256).hexdigest()
    url = f"/api/triggers/{pid}/trigger"
    json_type = {"Content-Type": "application/json"}
    bad = client.post(url, content=body, headers={"X-Hub-Signature-256": "sha256=00", **json_type})
    assert bad.status_code == 401
    ok = client.post(url, content=body, headers={"X-Hub-Signature-256": sig, **json_type})
    assert ok.status_code == 202 and ok.json()["status"] == "accepted"
    assert last_run(client, pid)["outputs"]["payload"] == {"event": "push"}


def test_inactive_and_unconfigured_triggers(env):
    client, svc, _ = env
    off = create(client, svc, "agenticmind.core.webhook_trigger", {"secret": ref("HOOK_TOKEN"), "enabled": False})
    assert client.get(f"/api/projects/{off}/triggers").json() == []
    assert client.post(f"/api/triggers/{off}/trigger", json={}).status_code == 404
    unset = create(client, svc, "agenticmind.core.webhook_trigger", {"secret": ref("NOT_SET")})
    [status] = client.get(f"/api/projects/{unset}/triggers").json()
    assert not status["active"] and "NOT_SET" in status["problem"]
    assert client.post(f"/api/triggers/{unset}/trigger", json={}).status_code == 503


# ---- Slack --------------------------------------------------------------------------------------
def test_slack_signature_matches_the_documented_example():
    body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V&"
            b"channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text=&"
            b"response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN&"
            b"trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")  # fmt: skip
    headers = {"x-slack-request-timestamp": "1531420618",
               "x-slack-signature": "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"}  # fmt: skip
    ch.verify_slack("8f742231b10e8888abcd99yyyzzz85a5", headers, body, now=1531420618)
    with pytest.raises(ch.Rejected):
        ch.verify_slack("8f742231b10e8888abcd99yyyzzz85a5", headers, body, now=1531420618 + 600)  # replayed


def slack_headers(body: bytes, secret: str = "fixture-slack-signing") -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"}


def test_slack_events_reply_in_thread_and_dedupe(env):
    client, svc, outbox = env
    cfg = {"channel": "slack", "slack_signing_secret": ref("SLACK_SIGNING"), "slack_bot_token": ref("SLACK_BOT")}
    pid = create(client, svc, "agenticmind.core.chat_trigger", cfg)
    url = f"/api/triggers/{pid}/trigger"
    challenge = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
    assert client.post(url, content=challenge, headers=slack_headers(challenge)).json() == {"challenge": "abc123"}
    assert client.post(url, content=challenge, headers=slack_headers(challenge, "wrong")).status_code == 401
    event = {"type": "event_callback", "event_id": "Ev1", "team_id": "T1", "event": {
        "type": "message", "channel": "C1", "user": "U1", "text": "status please", "ts": "100.1"}}  # fmt: skip
    body = json.dumps(event).encode()
    assert client.post(url, content=body, headers=slack_headers(body)).status_code == 200
    assert client.post(url, content=body, headers={**slack_headers(body), "X-Slack-Retry-Num": "1"}).status_code == 200
    bot = json.dumps({**event, "event_id": "Ev2", "event": {**event["event"], "bot_id": "B1"}}).encode()
    client.post(url, content=bot, headers=slack_headers(bot))
    wait_for(lambda: outbox.requests)
    time.sleep(0.3)
    [sent] = outbox.bodies()  # one reply: the retry was deduplicated and the bot message ignored
    assert sent == {"channel": "C1", "text": "You said: status please", "thread_ts": "100.1"}
    assert outbox.requests[0].headers["authorization"] == "Bearer fixture-slack-bot"
    assert len(client.get(f"/api/projects/{pid}/runs").json()) == 1


# ---- Telegram -----------------------------------------------------------------------------------
def test_telegram_secret_header_and_reply(env):
    client, svc, outbox = env
    cfg = {"channel": "telegram", "telegram_bot_token": ref("TG_BOT"), "telegram_secret_token": ref("TG_SECRET")}
    pid = create(client, svc, "agenticmind.core.chat_trigger", cfg)
    sender = {"id": 1, "is_bot": False, "username": "ada"}
    message = {"message_id": 5, "chat": {"id": 777, "type": "private"}, "from": sender, "text": "hi bot"}
    update = {"update_id": 9, "message": message}
    url = f"/api/triggers/{pid}/trigger"
    assert client.post(url, json=update).status_code == 401
    ok = client.post(url, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "fixture-telegram-header"})
    assert ok.status_code == 200
    wait_for(lambda: outbox.requests)
    assert str(outbox.requests[0].url) == "https://api.telegram.org/botfixture-telegram-bot/sendMessage"
    reply_parameters = {"message_id": 5, "allow_sending_without_reply": True}
    assert outbox.bodies()[0] == {"chat_id": 777, "text": "You said: hi bot", "reply_parameters": reply_parameters}
    run = last_run(client, pid)
    assert run["inputs"]["_trigger"]["sender"] == "ada" and run["inputs"]["_trigger"]["channel"] == "telegram"


# ---- WhatsApp -----------------------------------------------------------------------------------
def test_whatsapp_verification_signature_and_reply(env):
    client, svc, outbox = env
    pid = create(client, svc, "agenticmind.core.chat_trigger", {
        "channel": "whatsapp", "whatsapp_verify_token": ref("WA_VERIFY"), "whatsapp_app_secret": ref("WA_APP"),
        "whatsapp_access_token": ref("WA_TOKEN")})  # fmt: skip
    url = f"/api/triggers/{pid}/trigger"
    hub = {"hub.mode": "subscribe", "hub.challenge": "42"}
    ok = client.get(url, params={**hub, "hub.verify_token": "fixture-wa-verify"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get(url, params={**hub, "hub.verify_token": "no"}).status_code == 403
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"phone_number_id": "106540352242922"},
        "contacts": [{"profile": {"name": "Sheena"}, "wa_id": "16505551234"}],
        "messages": [{"from": "16505551234", "id": "wamid.X", "type": "text",
                      "text": {"body": "Another color?"}}]}}]}]}  # fmt: skip
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"fixture-wa-app-secret", body, hashlib.sha256).hexdigest()
    assert client.post(url, content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    assert client.post(url, content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
    wait_for(lambda: outbox.requests)
    assert str(outbox.requests[0].url) == "https://graph.facebook.com/v27.0/106540352242922/messages"
    assert outbox.bodies()[0] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "16505551234",
                                  "type": "text", "text": {"body": "You said: Another color?"},
                                  "context": {"message_id": "wamid.X"}}  # fmt: skip


# ---- schedule -----------------------------------------------------------------------------------
def test_schedule_fires_when_due(env):
    client, svc, _ = env
    pid = create(client, svc, "agenticmind.core.schedule_trigger",
                 {"mode": "interval", "interval_minutes": 5, "payload": {"report": "daily"}})  # fmt: skip
    [status] = client.get(f"/api/projects/{pid}/triggers").json()
    assert status["next_run_at"] is not None
    real_clock = svc.triggers.clock
    svc.triggers.clock = lambda: real_clock() + timedelta(minutes=6)
    client.portal.call(svc.triggers.tick)
    run = last_run(client, pid)
    assert run["outputs"]["payload"] == {"report": "daily"} and "scheduled_for" in run["inputs"]["_trigger"]
    client.portal.call(svc.triggers.tick)  # not due again yet
    assert len(client.get(f"/api/projects/{pid}/runs").json()) == 1


# ---- email --------------------------------------------------------------------------------------
class FakeImap:
    store_calls: list = []

    def __init__(self, host, port):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        assert password == "fixture-mail-password"

    def select(self, folder):
        pass

    def search(self, charset, criterion):
        return "OK", [b"1"] if not FakeImap.store_calls else [b""]

    def fetch(self, num, what):
        raw = (b"From: Ada <ada@example.com>\r\nTo: bot@example.com\r\nSubject: Invoice question\r\n"
               b"Message-ID: <m1@example.com>\r\n\r\nWhere is my invoice?\r\n")  # fmt: skip
        return "OK", [(b"1 (RFC822)", raw)]

    def store(self, num, flags, value):
        FakeImap.store_calls.append(num)


class FakeSmtp:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSmtp.sent.append(msg)


def test_email_poll_and_reply(env):
    client, svc, _ = env
    FakeImap.store_calls, FakeSmtp.sent = [], []
    svc.triggers.imap_factory, svc.triggers.smtp_factory = FakeImap, FakeSmtp
    pid = create(client, svc, "agenticmind.core.chat_trigger", {
        "channel": "email", "imap_host": "imap.example.com", "email_username": "bot@example.com",
        "email_password": ref("MAIL_PW"), "smtp_host": "smtp.example.com", "poll_seconds": 15})  # fmt: skip
    wait_for(lambda: FakeSmtp.sent)
    msg = FakeSmtp.sent[0]
    assert msg["To"] == "Ada <ada@example.com>" and msg["Subject"] == "Re: Invoice question"
    assert msg["In-Reply-To"] == "<m1@example.com>" and "You said: Where is my invoice?" in msg.get_content()
    assert FakeImap.store_calls == [b"1"]  # marked as seen: one run per message
    assert last_run(client, pid)["inputs"]["_trigger"]["payload"]["subject"] == "Invoice question"


# ---- queues -------------------------------------------------------------------------------------
class FakeRedis:
    def __init__(self):
        self.acked: list = []
        self.served = False

    async def xgroup_create(self, stream, group, id, mkstream):
        raise Exception("BUSYGROUP Consumer Group name already exists")

    async def xreadgroup(self, group, consumer, streams, count, block):
        if self.served:
            import asyncio

            await asyncio.sleep(0.05)
            return []
        self.served = True
        return [("agenticmind:events", [("1-0", {"data": json.dumps({"order_id": 7})})])]

    async def xack(self, stream, group, msg_id):
        self.acked.append(msg_id)


class FakeSqs:
    def __init__(self):
        self.deleted: list = []
        self.served = False

    def receive_message(self, **kw):
        if self.served:
            time.sleep(0.05)
            return {}
        self.served = True
        return {"Messages": [{"MessageId": "m-1", "ReceiptHandle": "rh-1", "Body": "plain text job"}]}

    def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803 - boto3's keyword names
        self.deleted.append(ReceiptHandle)


def test_redis_and_sqs_queues(env):
    client, svc, _ = env
    redis, sqs = FakeRedis(), FakeSqs()
    svc.triggers.redis_factory = lambda config: redis
    svc.triggers.sqs_factory = lambda config: sqs
    rid = create(client, svc, "agenticmind.core.queue_trigger", {"backend": "redis", "acknowledge": "on_success"})
    assert last_run(client, rid)["outputs"]["payload"] == {"order_id": 7}
    wait_for(lambda: redis.acked == ["1-0"])  # acknowledged after the run succeeded
    sid = create(client, svc, "agenticmind.core.queue_trigger",
                 {"backend": "sqs", "queue_url": "https://sqs.us-east-1.amazonaws.com/1/q"})  # fmt: skip
    assert last_run(client, sid)["outputs"]["answer"] == "You said: plain text job"
    wait_for(lambda: sqs.deleted == ["rh-1"])

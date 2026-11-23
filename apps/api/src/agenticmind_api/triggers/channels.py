"""Inbound verification, event parsing and replies for each trigger channel (ADR-0035).

Pure functions where possible (verification, parsing) so they are tested without a network. Senders take an
``httpx.AsyncClient`` so tests can mount a mock transport.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import hmac
import imaplib
import json
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

import httpx

SLACK_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
TELEGRAM_API = "https://api.telegram.org"
GRAPH_API = "https://graph.facebook.com"
SLACK_REPLAY_WINDOW = 60 * 5


@dataclass
class Inbound:
    """One event to start a run with. ``reply_to`` carries what a reply needs (never secrets)."""

    payload: Any
    text: str = ""
    sender: str | None = None
    event_id: str | None = None
    reply_to: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


class Rejected(Exception):
    """The request failed verification (wrong token / signature / stale timestamp)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hmac_sha256_hex(key: str, body: bytes) -> str:
    return hmac.new(key.encode(), body, hashlib.sha256).hexdigest()


# ---- generic webhook ----------------------------------------------------------------------------
def verify_webhook(auth: str, secret: str | None, headers: dict[str, str], body: bytes, signature_header: str) -> None:
    if auth == "none":
        return
    if not secret:
        raise Rejected(503, "This trigger's secret is not configured")
    if auth == "token":
        supplied = headers.get("x-webhook-token") or ""
        bearer = headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            supplied = supplied or bearer[7:].strip()
        if not supplied or not _equal(supplied, secret):
            raise Rejected(401, "Missing or wrong webhook token")
        return
    got = headers.get(signature_header.lower(), "")
    got = got.split("=", 1)[1] if got.lower().startswith("sha256=") else got
    if not got or not _equal(got.lower(), hmac_sha256_hex(secret, body)):
        raise Rejected(401, f"Missing or wrong HMAC signature in {signature_header}")


def parse_body(body: bytes, content_type: str) -> Any:
    if not body:
        return None
    if "json" in content_type or body[:1] in (b"{", b"["):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            pass
    if "x-www-form-urlencoded" in content_type:
        from urllib.parse import parse_qs

        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
    return body.decode("utf-8", "replace")


# ---- Slack --------------------------------------------------------------------------------------
def verify_slack(signing_secret: str | None, headers: dict[str, str], body: bytes, now: float | None = None) -> None:
    if not signing_secret:
        raise Rejected(503, "slack_signing_secret is not configured")
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts.isdigit() or abs((now or time.time()) - int(ts)) > SLACK_REPLAY_WINDOW:
        raise Rejected(401, "Stale or missing Slack timestamp")
    expected = "v0=" + hmac_sha256_hex(signing_secret, b"v0:" + ts.encode() + b":" + body)
    if not _equal(sig, expected):
        raise Rejected(401, "Wrong Slack signature")


def parse_slack(data: dict[str, Any]) -> Inbound | None:
    """A user message (bot messages, edits and other subtypes are ignored)."""
    if data.get("type") != "event_callback":
        return None
    ev = data.get("event") or {}
    if ev.get("type") not in ("message", "app_mention") or ev.get("bot_id") or ev.get("subtype"):
        return None
    text = str(ev.get("text") or "")
    if not text.strip():
        return None
    return Inbound(
        payload=ev,
        text=text,
        sender=ev.get("user"),
        event_id=data.get("event_id"),
        reply_to={"channel": ev.get("channel"), "thread_ts": ev.get("thread_ts") or ev.get("ts")},
        meta={"channel_type": ev.get("channel_type"), "team_id": data.get("team_id")},
    )


async def send_slack(http: httpx.AsyncClient, token: str, reply_to: dict[str, Any], text: str, in_thread: bool) -> None:
    body: dict[str, Any] = {"channel": reply_to["channel"], "text": text[:39_000]}
    if in_thread and reply_to.get("thread_ts"):
        body["thread_ts"] = reply_to["thread_ts"]
    r = await http.post(SLACK_POST_MESSAGE, json=body, headers={"Authorization": f"Bearer {token}"})
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or not data.get("ok"):
        raise RuntimeError(f"Slack chat.postMessage failed: {data.get('error') or r.status_code}")


# ---- Telegram -----------------------------------------------------------------------------------
def verify_telegram(secret_token: str | None, headers: dict[str, str]) -> None:
    if not secret_token:
        raise Rejected(503, "telegram_secret_token is not configured")
    if not _equal(headers.get("x-telegram-bot-api-secret-token", ""), secret_token):
        raise Rejected(401, "Wrong X-Telegram-Bot-Api-Secret-Token")


def parse_telegram(data: dict[str, Any]) -> Inbound | None:
    msg = data.get("message") or {}
    sender = msg.get("from") or {}
    if not msg.get("text") or sender.get("is_bot"):
        return None
    chat = msg.get("chat") or {}
    return Inbound(
        payload=msg,
        text=str(msg["text"]),
        sender=str(sender.get("username") or sender.get("id") or ""),
        event_id=str(data.get("update_id")),
        reply_to={"chat_id": chat.get("id"), "message_id": msg.get("message_id")},
        meta={"chat_type": chat.get("type")},
    )


async def send_telegram(http: httpx.AsyncClient, token: str, reply_to: dict[str, Any], text: str) -> None:
    body: dict[str, Any] = {"chat_id": reply_to["chat_id"], "text": text[:4096]}
    if reply_to.get("message_id"):
        body["reply_parameters"] = {"message_id": reply_to["message_id"], "allow_sending_without_reply": True}
    r = await http.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram sendMessage failed: HTTP {r.status_code}")  # never echo the URL (token)


# ---- WhatsApp Cloud API -------------------------------------------------------------------------
def whatsapp_challenge(verify_token: str | None, params: dict[str, str]) -> str:
    if not verify_token:
        raise Rejected(503, "whatsapp_verify_token is not configured")
    if params.get("hub.mode") != "subscribe" or not _equal(params.get("hub.verify_token", ""), verify_token):
        raise Rejected(403, "Wrong verify token")
    return params.get("hub.challenge", "")


def verify_whatsapp(app_secret: str | None, headers: dict[str, str], body: bytes) -> None:
    if not app_secret:
        raise Rejected(503, "whatsapp_app_secret is not configured")
    got = headers.get("x-hub-signature-256", "")
    if not got.startswith("sha256=") or not _equal(got[7:].lower(), hmac_sha256_hex(app_secret, body)):
        raise Rejected(401, "Wrong X-Hub-Signature-256")


def parse_whatsapp(data: dict[str, Any]) -> list[Inbound]:
    found: list[Inbound] = []
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            phone_id = (value.get("metadata") or {}).get("phone_number_id")
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                if m.get("type") != "text":
                    continue
                sender = m.get("from") or m.get("from_user_id")
                found.append(Inbound(
                    payload=m,
                    text=str((m.get("text") or {}).get("body") or ""),
                    sender=sender,
                    event_id=m.get("id"),
                    reply_to={"phone_number_id": phone_id, "to": m.get("from"), "message_id": m.get("id")},
                    meta={"name": names.get(m.get("from"))},
                ))  # fmt: skip
    return found


async def send_whatsapp(http: httpx.AsyncClient, token: str, version: str, reply_to: dict[str, Any], text: str) -> None:
    if not reply_to.get("to"):
        raise RuntimeError("WhatsApp reply needs the sender's phone number (not provided by this message)")
    body: dict[str, Any] = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": reply_to["to"],
                            "type": "text", "text": {"body": text[:4096]}}  # fmt: skip
    if reply_to.get("message_id"):
        body["context"] = {"message_id": reply_to["message_id"]}
    url = f"{GRAPH_API}/{version}/{reply_to['phone_number_id']}/messages"
    r = await http.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    if r.status_code >= 400:
        raise RuntimeError(f"WhatsApp send failed: HTTP {r.status_code} {r.text[:200]}")


# ---- Email (blocking stdlib clients; call from a thread) ----------------------------------------
def _plain_text(msg: email.message.Message) -> str:
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        import re

        text = re.sub(r"<[^>]+>", " ", text)
    return text.strip()


def fetch_unseen(host: str, port: int, username: str, password: str, folder: str, limit: int = 20,
                 imap_factory: Any = imaplib.IMAP4_SSL) -> list[Inbound]:  # fmt: skip
    """Unseen messages, marked as seen once read (so each starts one run)."""
    found: list[Inbound] = []
    with imap_factory(host, port) as imap:
        imap.login(username, password)
        imap.select(folder)
        _typ, data = imap.search(None, "UNSEEN")
        for num in (data[0].split() if data and data[0] else [])[:limit]:
            _typ, parts = imap.fetch(num, "(RFC822)")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            imap.store(num, "+FLAGS", "\\Seen")
            subject = str(msg.get("Subject", ""))
            sender = str(msg.get("Reply-To") or msg.get("From") or "")
            text = _plain_text(msg)
            found.append(Inbound(
                payload={"from": sender, "to": str(msg.get("To", "")), "subject": subject, "body": text,
                         "message_id": str(msg.get("Message-ID", ""))},
                text=text or subject,
                sender=sender,
                event_id=str(msg.get("Message-ID") or num),
                reply_to={"to": sender, "subject": subject, "message_id": str(msg.get("Message-ID", ""))},
            ))  # fmt: skip
    return found


def send_email(host: str, port: int, username: str, password: str, sender: str, reply_to: dict[str, Any],
               text: str, smtp_factory: Any = smtplib.SMTP) -> None:  # fmt: skip
    msg = EmailMessage()
    subject = reply_to.get("subject") or ""
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    msg["From"] = sender or username
    msg["To"] = reply_to["to"]
    if reply_to.get("message_id"):
        msg["In-Reply-To"] = reply_to["message_id"]
        msg["References"] = reply_to["message_id"]
    msg.set_content(text)
    with smtp_factory(host, port, timeout=30) as smtp:
        if port != 465:
            smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)

"""Trigger nodes (ADR-0035): entry points that start a run when something happens.

A trigger node outputs the event that started the run. The API's trigger service activates the enabled
trigger nodes of each project's latest saved revision (webhook endpoints, a scheduler, queue and mailbox
pollers) and starts runs with the event in the run input under ``_trigger``. When a graph is run by hand
(Run button, CLI, tests) the node outputs its ``sample`` event instead, so triggers are testable anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from agenticmind_node_sdk import (
    NodeConfig,
    NodeContext,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    define_node,
    secret_field,
    ui,
)
from pydantic import Field, JsonValue, field_validator

from .io import PACKAGE

TRIGGER_INPUT_KEY = "_trigger"
TRIGGER_TYPES = {
    "agenticmind.core.webhook_trigger": "webhook",
    "agenticmind.core.schedule_trigger": "schedule",
    "agenticmind.core.queue_trigger": "queue",
    "agenticmind.core.chat_trigger": "chat",
}
_OUTPUTS = [
    OutputPort(name="payload", type=PortType.ANY, description="The event's data (request body, message, …)"),
    OutputPort(name="text", type=PortType.STRING, description="The event as text (a chat message's text)"),
    OutputPort(name="event", type=PortType.JSON, description="Metadata: kind, source, sender, received_at, …"),
]


def _enabled_field() -> Any:
    return Field(default=True, description="Listen for events (applies to the saved revision).")


def _emit(ctx: NodeContext, kind: str, sample: Any, sample_text: str = "") -> NodeResult:
    event = ctx.run_input.get(TRIGGER_INPUT_KEY)
    if isinstance(event, dict) and event.get("node") == ctx.node_id:
        payload = event.get("payload")
        meta = {k: v for k, v in event.items() if k not in ("payload", "text")}
        text = event.get("text")
        simulated = False
    else:
        payload, meta, text, simulated = sample, {"kind": kind}, None, True
    if not text:
        text = sample_text if simulated and sample_text else (payload if isinstance(payload, str) else "")
        if not text and payload is not None and not isinstance(payload, str):
            import json

            text = json.dumps(payload, ensure_ascii=False)
    meta.setdefault("kind", kind)
    meta.setdefault("received_at", datetime.now(UTC).isoformat())
    meta["simulated"] = simulated
    return NodeResult(outputs={"payload": payload, "text": str(text), "event": meta})


# ---- Webhook ------------------------------------------------------------------------------------
class WebhookTriggerConfig(NodeConfig):
    enabled: bool = _enabled_field()
    auth: Literal["token", "hmac", "none"] = Field(
        default="token",
        description="token: callers send 'Authorization: Bearer <secret>' (or X-Webhook-Token). hmac: an "
        "HMAC-SHA256 of the raw body in a header (GitHub-style 'sha256=<hex>'). none: anyone with the URL.",
    )
    secret: SecretRef | None = secret_field("Token or HMAC key, e.g. MY_WEBHOOK_SECRET")
    signature_header: str = Field(default="X-Hub-Signature-256", json_schema_extra=ui(show_when={"auth": ["hmac"]}))
    respond: Literal["ack", "wait"] = Field(
        default="ack", description="ack: answer 202 at once. wait: run, then answer with the run's outputs."
    )
    response_output: str = Field(
        default="", description="With wait: return only this output key (empty = all outputs).",
        json_schema_extra=ui(show_when={"respond": ["wait"]}),
    )  # fmt: skip
    wait_seconds: float = Field(default=30, ge=1, le=300, json_schema_extra=ui(show_when={"respond": ["wait"]}))
    sample: JsonValue = Field(default={"hello": "world"}, description="Payload used when you run the graph by hand.")


@define_node(
    type="agenticmind.core.webhook_trigger",
    version="1.0.0",
    display_name="Webhook Trigger",
    category="input",
    package=PACKAGE,
    description="Starts a run when an HTTP request arrives at this node's URL (token or HMAC protected).",
    outputs=_OUTPUTS,
    config=WebhookTriggerConfig,
    permissions=Permissions(credentials=True),
    tags=["entry", "trigger", "webhook"],
)
async def webhook_trigger(ctx: NodeContext, inputs, config: WebhookTriggerConfig) -> NodeResult:
    return _emit(ctx, "webhook", config.sample)


# ---- Schedule -----------------------------------------------------------------------------------
class ScheduleTriggerConfig(NodeConfig):
    enabled: bool = _enabled_field()
    mode: Literal["cron", "interval"] = "cron"
    cron: str = Field(
        default="0 9 * * 1-5", description="minute hour day-of-month month day-of-week, e.g. '*/15 * * * *'.",
        json_schema_extra=ui(show_when={"mode": ["cron"]}),
    )  # fmt: skip
    timezone: str = Field(default="UTC", description="IANA time zone for the cron expression, e.g. Europe/London.",
                          json_schema_extra=ui(show_when={"mode": ["cron"]}))  # fmt: skip
    interval_minutes: float = Field(default=60, ge=1, le=60 * 24 * 31,
                                    json_schema_extra=ui(show_when={"mode": ["interval"]}))  # fmt: skip
    payload: JsonValue = Field(default=None, description="Sent as the event payload on every run.")

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        from .cron import CronExpression

        CronExpression.parse(value)
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r}") from exc
        return value


@define_node(
    type="agenticmind.core.schedule_trigger",
    version="1.0.0",
    display_name="Schedule Trigger",
    category="input",
    package=PACKAGE,
    description="Starts a run on a schedule (cron expression with a time zone, or every N minutes).",
    outputs=_OUTPUTS,
    config=ScheduleTriggerConfig,
    tags=["entry", "trigger", "schedule", "cron"],
)
async def schedule_trigger(ctx: NodeContext, inputs, config: ScheduleTriggerConfig) -> NodeResult:
    return _emit(ctx, "schedule", config.payload)


# ---- Queue --------------------------------------------------------------------------------------
_REDIS = {"backend": ["redis"]}
_SQS = {"backend": ["sqs"]}


class QueueTriggerConfig(NodeConfig):
    enabled: bool = _enabled_field()
    backend: Literal["redis", "sqs"] = Field(default="redis", description="Redis Streams or Amazon SQS.")
    redis_url: str = Field(default="redis://localhost:6379/0", json_schema_extra=ui(show_when=_REDIS))
    redis_password: SecretRef | None = secret_field("Redis password (if not in the URL)", show_when=_REDIS)
    stream: str = Field(default="agenticmind:events", json_schema_extra=ui(show_when=_REDIS))
    group: str = Field(default="agenticmind", description="Consumer group (created if missing).",
                       json_schema_extra=ui(show_when=_REDIS))  # fmt: skip
    queue_url: str = Field(default="", description="https://sqs.<region>.amazonaws.com/<account>/<queue>",
                           json_schema_extra=ui(show_when=_SQS))  # fmt: skip
    region: str = Field(default="us-east-1", json_schema_extra=ui(show_when=_SQS))
    access_key_id: SecretRef | None = secret_field(
        "AWS access key id (empty = default AWS credentials)", show_when=_SQS
    )
    secret_access_key: SecretRef | None = secret_field("AWS secret access key", show_when=_SQS)
    endpoint_url: str | None = Field(default=None, description="For SQS-compatible services (e.g. LocalStack).",
                                     json_schema_extra=ui(show_when=_SQS, advanced=True))  # fmt: skip
    max_messages: int = Field(default=10, ge=1, le=10, json_schema_extra=ui(advanced=True))
    acknowledge: Literal["on_start", "on_success"] = Field(
        default="on_start",
        description="on_start: remove the message once its run starts. on_success: only after the run succeeds "
        "(a failed run leaves it for redelivery).",
    )
    sample: JsonValue = Field(default={"order_id": 42, "status": "paid"})


@define_node(
    type="agenticmind.core.queue_trigger",
    version="1.0.0",
    display_name="Queue Trigger",
    category="input",
    package=PACKAGE,
    description="Starts a run for each message on a Redis Stream or an Amazon SQS queue.",
    outputs=_OUTPUTS,
    config=QueueTriggerConfig,
    permissions=Permissions(network=True, credentials=True),
    tags=["entry", "trigger", "queue", "redis", "sqs"],
)
async def queue_trigger(ctx: NodeContext, inputs, config: QueueTriggerConfig) -> NodeResult:
    return _emit(ctx, "queue", config.sample)


# ---- Chat ---------------------------------------------------------------------------------------
_SLACK = {"channel": ["slack"]}
_TELEGRAM = {"channel": ["telegram"]}
_WHATSAPP = {"channel": ["whatsapp"]}
_EMAIL = {"channel": ["email"]}


class ChatTriggerConfig(NodeConfig):
    enabled: bool = _enabled_field()
    channel: Literal["slack", "telegram", "whatsapp", "email"] = "telegram"
    reply_output: str = Field(default="answer", description="Run output sent back as the reply (empty = do not reply).")
    error_reply: str = Field(default="Sorry, something went wrong. Please try again later.",
                             json_schema_extra=ui(advanced=True))  # fmt: skip
    # Slack
    slack_signing_secret: SecretRef | None = secret_field("Slack app signing secret", show_when=_SLACK)
    slack_bot_token: SecretRef | None = secret_field("Slack bot token (xoxb-…) for replies", show_when=_SLACK)
    slack_reply_in_thread: bool = Field(default=True, json_schema_extra=ui(show_when=_SLACK))
    # Telegram
    telegram_bot_token: SecretRef | None = secret_field("Telegram bot token from @BotFather", show_when=_TELEGRAM)
    telegram_secret_token: SecretRef | None = secret_field(
        "Secret checked in the X-Telegram-Bot-Api-Secret-Token header (set with setWebhook)", show_when=_TELEGRAM
    )
    # WhatsApp (Cloud API)
    whatsapp_verify_token: SecretRef | None = secret_field(
        "Webhook verify token you chose in Meta", show_when=_WHATSAPP
    )
    whatsapp_app_secret: SecretRef | None = secret_field("Meta app secret (signature check)", show_when=_WHATSAPP)
    whatsapp_access_token: SecretRef | None = secret_field("Access token for sending replies", show_when=_WHATSAPP)
    whatsapp_graph_version: str = Field(default="v27.0", json_schema_extra=ui(show_when=_WHATSAPP, advanced=True))
    # Email (IMAP in, SMTP out)
    imap_host: str = Field(default="", json_schema_extra=ui(show_when=_EMAIL, placeholder="imap.example.com"))
    imap_port: int = Field(default=993, json_schema_extra=ui(show_when=_EMAIL, advanced=True))
    email_username: str = Field(default="", json_schema_extra=ui(show_when=_EMAIL))
    email_password: SecretRef | None = secret_field("Mailbox password or app password", show_when=_EMAIL)
    imap_folder: str = Field(default="INBOX", json_schema_extra=ui(show_when=_EMAIL, advanced=True))
    poll_seconds: int = Field(default=60, ge=15, le=3600, json_schema_extra=ui(show_when=_EMAIL))
    smtp_host: str = Field(default="", json_schema_extra=ui(show_when=_EMAIL, placeholder="smtp.example.com"))
    smtp_port: int = Field(default=587, json_schema_extra=ui(show_when=_EMAIL, advanced=True))
    from_address: str = Field(default="", description="Reply sender (default: the username).",
                              json_schema_extra=ui(show_when=_EMAIL, advanced=True))  # fmt: skip
    sample_text: str = Field(default="Hi! What can you do?", description="Message used when you run by hand.")


@define_node(
    type="agenticmind.core.chat_trigger",
    version="1.0.0",
    display_name="Chat Trigger",
    category="input",
    package=PACKAGE,
    description="Starts a run for each Slack, Telegram, WhatsApp or email message, and replies with a run output.",
    outputs=_OUTPUTS,
    config=ChatTriggerConfig,
    permissions=Permissions(network=True, credentials=True),
    tags=["entry", "trigger", "chat", "slack", "telegram", "whatsapp", "email"],
)
async def chat_trigger(ctx: NodeContext, inputs, config: ChatTriggerConfig) -> NodeResult:
    sample = {"channel": config.channel, "text": config.sample_text, "sender": "sample-user"}
    return _emit(ctx, "chat", sample, config.sample_text)

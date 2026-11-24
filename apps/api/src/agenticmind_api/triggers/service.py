"""The trigger service (ADR-0035): activates trigger nodes and turns events into runs.

* **Activation:** the enabled trigger nodes of every project's *latest saved revision*. Rescanned at start-up,
  after a project is saved or deleted, and every minute as a safety net.
* **Webhooks and chat webhooks** arrive at ``/api/triggers/{project}/{node}`` (see ``routers/triggers.py``).
* **Schedules** are checked every few seconds; the next due time is kept in memory (a restart does not
  replay missed runs).
* **Queues and mailboxes** are polled by one background task per trigger.
* Every event starts an ordinary recorded run of that revision with the event in the run input
  (``_trigger``). Chat triggers then send a run output back as the reply.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from agenticmind_ir import load_project
from agenticmind_nodes_core.cron import CronExpression
from agenticmind_nodes_core.triggers import TRIGGER_INPUT_KEY, TRIGGER_TYPES
from agenticmind_runtime import PlanError

from . import channels as ch

if TYPE_CHECKING:
    from ..deps import Services

log = logging.getLogger("agenticmind.api.triggers")
TICK_SECONDS = 5.0
RESCAN_SECONDS = 60.0
POLL_BACKOFF_SECONDS = 30.0
REPLY_WAIT_SECONDS = 600.0


@dataclass
class ActiveTrigger:
    project_id: str
    node_id: str
    kind: str
    config: Any
    revision: Any
    doc: Any
    problem: str | None = None
    next_run_at: datetime | None = None
    last_event_at: datetime | None = None
    last_run_id: str | None = None
    last_error: str | None = None
    events: int = 0
    seen: deque[str] = field(default_factory=lambda: deque(maxlen=2000))
    task: asyncio.Task[None] | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.project_id, self.node_id)

    @property
    def fingerprint(self) -> str:
        return f"{self.revision.revision}:{json.dumps(self.config.model_dump(mode='json'), sort_keys=True)}"

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "node_id": self.node_id,
            "kind": self.kind,
            "revision": self.revision.revision,
            "active": self.problem is None,
            "problem": self.problem,
            "events": self.events,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "last_run_id": self.last_run_id,
            "last_error": self.last_error,
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
        }
        if self.kind in ("webhook", "chat"):
            out["path"] = f"/api/triggers/{self.project_id}/{self.node_id}"
        if self.kind == "chat":
            out["channel"] = self.config.channel
        return out


def _now() -> datetime:
    return datetime.now(UTC)


class TriggerService:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.active: dict[tuple[str, str], ActiveTrigger] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._main: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        # replaceable in tests
        self.http_transport: httpx.AsyncBaseTransport | None = None
        self.imap_factory: Any = None
        self.smtp_factory: Any = None
        self.redis_factory: Any = None
        self.sqs_factory: Any = None
        self.clock = _now
        #: Only the process holding the "triggers" lease runs schedules and pollers (ADR-0039); webhooks
        #: are accepted by every process (they only queue runs).
        self.leader = False

    # ---- lifecycle --------------------------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await asyncio.to_thread(self._scan)
        await self._elect()
        self._apply_tasks()
        self._main = asyncio.create_task(self._run(), name="triggers")

    async def stop(self) -> None:
        tasks = [t for t in (self._main, *(a.task for a in self.active.values())) if t is not None]
        tasks += list(self._background)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()
        if self.leader:
            await asyncio.to_thread(self.svc.store.release_lease, "triggers", self.svc.runs.worker_id)
            self.leader = False

    def request_refresh(self) -> None:
        """Thread-safe: ask the service to rescan (called after a project is saved or deleted)."""
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._wake.set)

    async def refresh(self) -> None:
        await asyncio.to_thread(self._scan)
        await self._elect()
        self._apply_tasks()

    async def _elect(self) -> None:
        runs = self.svc.runs
        owner = runs.worker_id
        lease = max(runs.lease_seconds, TICK_SECONDS * 3)
        try:
            leader = await asyncio.to_thread(self.svc.store.acquire_lease, "triggers", owner, lease)
        except Exception:  # noqa: BLE001 - keep the previous role on a database hiccup
            log.exception("trigger leader election failed")
            return
        if leader != self.leader:
            log.info("trigger scheduler %s on %s", "started" if leader else "handed over", owner)
        self.leader = leader
        if not leader:  # another process schedules and polls: stop ours
            for trig in self.active.values():
                if trig.task is not None:
                    trig.task.cancel()
                    trig.task = None
                trig.next_run_at = None

    async def _run(self) -> None:
        last_scan = self.clock()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)
            except TimeoutError:
                pass
            try:
                if self._wake.is_set() or (self.clock() - last_scan).total_seconds() >= RESCAN_SECONDS:
                    self._wake.clear()
                    await self.refresh()
                    last_scan = self.clock()
                else:
                    await self._elect()
                    self._apply_tasks()
                if self.leader:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive one bad project
                log.exception("trigger loop error")

    # ---- activation -------------------------------------------------------------------------
    def _scan(self) -> None:
        found: dict[tuple[str, str], ActiveTrigger] = {}
        for project in self.svc.store.list_projects():
            rev = self.svc.store.get_revision(project.id)
            if rev is None:
                continue
            try:
                doc = load_project(rev.document)
            except Exception:  # noqa: BLE001
                continue
            for node in doc.nodes:
                kind = TRIGGER_TYPES.get(node.type)
                if kind is None:
                    continue
                entry = self.svc.registry.get(node.type, node.version)
                if entry is None:
                    continue
                try:
                    config = entry.definition.parse_config(node.config)
                except Exception as exc:  # noqa: BLE001 - shown in the trigger status
                    config = None
                    problem = f"invalid config: {exc}"
                else:
                    if not config.enabled:
                        continue
                    problem = self._problem(kind, config)
                if config is None:
                    continue
                trig = ActiveTrigger(project.id, node.id, kind, config, rev, doc, problem=problem)
                old = self.active.get(trig.key)
                if old is not None and old.fingerprint == trig.fingerprint:
                    found[trig.key] = old  # unchanged: keep counters, schedule and poller
                    continue
                found[trig.key] = trig
        for key, old in self.active.items():
            if key not in found or found[key] is not old:
                if old.task is not None:
                    old.task.cancel()
        self.active = found

    def _problem(self, kind: str, c: Any) -> str | None:
        """Why a trigger cannot work yet (missing settings / secrets), or None."""
        missing: list[str] = []

        def need(attr: str) -> None:
            ref = getattr(c, attr, None)
            if ref is None or not self.svc.secrets.get(ref.name):
                missing.append(attr if ref is None else f"secret {ref.name}")

        if kind == "webhook" and c.auth != "none":
            need("secret")
        elif kind == "chat":
            if c.channel == "slack":
                need("slack_signing_secret")
                if c.reply_output:
                    need("slack_bot_token")
            elif c.channel == "telegram":
                need("telegram_secret_token")
                if c.reply_output:
                    need("telegram_bot_token")
            elif c.channel == "whatsapp":
                need("whatsapp_verify_token")
                need("whatsapp_app_secret")
                if c.reply_output:
                    need("whatsapp_access_token")
            else:
                for attr in ("imap_host", "email_username"):
                    if not getattr(c, attr):
                        missing.append(attr)
                need("email_password")
                if c.reply_output and not c.smtp_host:
                    missing.append("smtp_host")
        elif kind == "queue" and c.backend == "sqs" and not c.queue_url:
            missing.append("queue_url")
        return f"needs {', '.join(missing)}" if missing else None

    def _apply_tasks(self) -> None:
        if not self.leader:
            return
        for trig in self.active.values():
            if trig.problem is not None:
                continue
            if trig.kind == "schedule" and trig.next_run_at is None:
                trig.next_run_at = self._next_time(trig, self.clock())
            needs_poller = trig.kind == "queue" or (trig.kind == "chat" and trig.config.channel == "email")
            if needs_poller and (trig.task is None or trig.task.done()):
                trig.task = asyncio.create_task(self._poll(trig), name=f"trigger:{trig.project_id}:{trig.node_id}")

    def secret(self, ref: Any) -> str | None:
        return self.svc.secrets.get(ref.name) if ref is not None else None

    def status(self, project_id: str) -> list[dict[str, Any]]:
        return [t.public() for t in self.active.values() if t.project_id == project_id]

    def get(self, project_id: str, node_id: str) -> ActiveTrigger | None:
        return self.active.get((project_id, node_id))

    # ---- schedules --------------------------------------------------------------------------
    def _next_time(self, trig: ActiveTrigger, after: datetime) -> datetime:
        c = trig.config
        if c.mode == "interval":
            return after + timedelta(minutes=c.interval_minutes)
        return CronExpression.parse(c.cron).next_after(after, c.timezone)

    async def tick(self) -> None:
        now = self.clock()
        for trig in list(self.active.values()):
            if trig.kind != "schedule" or trig.problem or trig.next_run_at is None or trig.next_run_at > now:
                continue
            due = trig.next_run_at
            trig.next_run_at = self._next_time(trig, now)
            await self.fire(trig, ch.Inbound(payload=trig.config.payload, meta={"scheduled_for": due.isoformat()}))

    # ---- runs and replies -------------------------------------------------------------------
    async def fire(self, trig: ActiveTrigger, inbound: ch.Inbound, *, reply: bool = True) -> str | None:
        """Start a run for one event (deduplicated by event id). Returns the run id."""
        if inbound.event_id:
            if inbound.event_id in trig.seen:
                return None
            trig.seen.append(inbound.event_id)
        event = {
            "node": trig.node_id,
            "kind": trig.kind,
            "payload": inbound.payload,
            "text": inbound.text,
            "sender": inbound.sender,
            "event_id": inbound.event_id,
            "received_at": self.clock().isoformat(),
            **({"channel": trig.config.channel} if trig.kind == "chat" else {}),
            **inbound.meta,
        }
        trig.last_event_at = self.clock()
        trig.events += 1
        try:
            plan = self.svc.runs.plan(trig.doc)
        except PlanError as exc:
            trig.last_error = f"graph is not executable: {exc}"
            return None
        run_id = self.svc.runs.start(trig.project_id, trig.revision, plan, {TRIGGER_INPUT_KEY: event})
        trig.last_run_id = run_id
        trig.last_error = None
        if reply and trig.kind == "chat" and trig.config.reply_output:
            self._spawn(self._reply_when_done(trig, run_id, inbound))
        return run_id

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def wait_outputs(self, run_id: str, timeout: float) -> tuple[str, dict[str, Any], Any]:
        """(status, outputs, error) once the run ends or the timeout passes (status 'running')."""
        try:
            await self.svc.runs.wait(run_id, timeout=timeout)
        except TimeoutError:
            return "running", {}, None
        row = self.svc.store.get_run(run_id)
        if row is None:
            return "failed", {}, {"message": "run record missing"}
        return row.status, row.outputs or {}, row.error

    async def _reply_when_done(self, trig: ActiveTrigger, run_id: str, inbound: ch.Inbound) -> None:
        status, outputs, _error = await self.wait_outputs(run_id, REPLY_WAIT_SECONDS)
        c = trig.config
        if status == "succeeded":
            value = outputs.get(c.reply_output)
            if value is None:
                trig.last_error = f"run {run_id} has no output '{c.reply_output}' to reply with"
                return
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        elif status in ("failed", "cancelled"):
            text = c.error_reply
        else:
            return  # still running or waiting for approval: no reply
        try:
            await self.send_reply(trig, inbound.reply_to, text)
        except Exception as exc:  # noqa: BLE001 - recorded in the trigger status
            trig.last_error = f"reply failed: {exc}"
            log.warning("trigger %s/%s: reply failed: %s", trig.project_id, trig.node_id, exc)

    async def send_reply(self, trig: ActiveTrigger, reply_to: dict[str, Any], text: str) -> None:
        c = trig.config
        if c.channel == "email":
            password = self.secret(c.email_password) or ""
            await asyncio.to_thread(
                ch.send_email, c.smtp_host, c.smtp_port, c.email_username, password,
                c.from_address or c.email_username, reply_to, text,
                **({"smtp_factory": self.smtp_factory} if self.smtp_factory else {}),
            )  # fmt: skip
            return
        async with httpx.AsyncClient(timeout=30, transport=self.http_transport) as http:
            if c.channel == "slack":
                await ch.send_slack(http, self.secret(c.slack_bot_token) or "", reply_to, text, c.slack_reply_in_thread)
            elif c.channel == "telegram":
                await ch.send_telegram(http, self.secret(c.telegram_bot_token) or "", reply_to, text)
            elif c.channel == "whatsapp":
                await ch.send_whatsapp(http, self.secret(c.whatsapp_access_token) or "", c.whatsapp_graph_version,
                                       reply_to, text)  # fmt: skip

    # ---- pollers ----------------------------------------------------------------------------
    async def _poll(self, trig: ActiveTrigger) -> None:
        while True:
            try:
                if trig.kind == "chat":
                    await self._poll_email(trig)
                elif trig.config.backend == "redis":
                    await self._poll_redis(trig)
                else:
                    await self._poll_sqs(trig)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                trig.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("trigger %s/%s poller: %s", trig.project_id, trig.node_id, trig.last_error)
                await asyncio.sleep(POLL_BACKOFF_SECONDS)

    async def _poll_email(self, trig: ActiveTrigger) -> None:
        c = trig.config
        kwargs = {"imap_factory": self.imap_factory} if self.imap_factory else {}
        while True:
            messages = await asyncio.to_thread(
                ch.fetch_unseen, c.imap_host, c.imap_port, c.email_username, self.secret(c.email_password) or "",
                c.imap_folder, **kwargs,
            )  # fmt: skip
            for inbound in messages:
                await self.fire(trig, inbound)
            trig.last_error = None
            await asyncio.sleep(c.poll_seconds)

    async def _ack_later(self, trig: ActiveTrigger, run_id: str | None, ack: Any) -> None:
        if run_id is None:
            return
        if trig.config.acknowledge == "on_start":
            await ack()
            return
        status, _o, _e = await self.wait_outputs(run_id, REPLY_WAIT_SECONDS)
        if status == "succeeded":
            await ack()

    async def _poll_redis(self, trig: ActiveTrigger) -> None:
        c = trig.config
        if self.redis_factory is not None:
            client = self.redis_factory(c)
        else:
            import redis.asyncio as aioredis

            client = aioredis.from_url(c.redis_url, password=self.secret(c.redis_password), decode_responses=True)
        consumer = f"agenticmind-{trig.node_id}"
        try:
            try:
                await client.xgroup_create(c.stream, c.group, id="$", mkstream=True)
            except Exception as exc:  # noqa: BLE001 - BUSYGROUP: the group already exists
                if "BUSYGROUP" not in str(exc):
                    raise
            while True:
                batches = await client.xreadgroup(c.group, consumer, {c.stream: ">"}, count=c.max_messages, block=5000)
                for _stream, entries in batches or []:
                    for msg_id, fields in entries:
                        payload = _decode_fields(fields)
                        text = payload if isinstance(payload, str) else ""
                        inbound = ch.Inbound(payload=payload, text=text, event_id=f"redis:{msg_id}",
                                             meta={"message_id": msg_id})  # fmt: skip
                        run_id = await self.fire(trig, inbound)

                        async def ack(msg_id: str = msg_id) -> None:
                            await client.xack(c.stream, c.group, msg_id)

                        self._spawn(self._ack_later(trig, run_id, ack))
                trig.last_error = None
        finally:
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    async def _poll_sqs(self, trig: ActiveTrigger) -> None:
        c = trig.config
        if self.sqs_factory is not None:
            client = self.sqs_factory(c)
        else:
            import boto3

            client = boto3.client(
                "sqs", region_name=c.region, endpoint_url=c.endpoint_url or None,
                aws_access_key_id=self.secret(c.access_key_id), aws_secret_access_key=self.secret(c.secret_access_key),
            )  # fmt: skip
        while True:
            resp = await asyncio.to_thread(
                client.receive_message, QueueUrl=c.queue_url, MaxNumberOfMessages=c.max_messages, WaitTimeSeconds=20
            )
            for m in resp.get("Messages", []):
                body = m.get("Body", "")
                try:
                    payload: Any = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    payload = body
                msg_id = m.get("MessageId")
                inbound = ch.Inbound(payload=payload, text=body if isinstance(payload, str) else "",
                                     event_id=f"sqs:{msg_id}", meta={"message_id": msg_id})  # fmt: skip
                run_id = await self.fire(trig, inbound)

                async def ack(handle: str = m["ReceiptHandle"]) -> None:
                    await asyncio.to_thread(client.delete_message, QueueUrl=c.queue_url, ReceiptHandle=handle)

                self._spawn(self._ack_later(trig, run_id, ack))
            trig.last_error = None


def _decode_fields(fields: dict[str, Any]) -> Any:
    """A Redis Stream entry as a payload: one JSON field (data/payload/body/json) is parsed, else the fields."""
    for key in ("data", "payload", "body", "json"):
        if key in fields and len(fields) == 1:
            value = fields[key]
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
    return dict(fields)

"""Structured trace events and run observers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from agenticmind_node_sdk import ModelUsage
from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self not in (NodeStatus.PENDING, NodeStatus.RUNNING)


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_SUCCEEDED = "run_succeeded"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    NODE_STARTED = "node_started"
    NODE_SUCCEEDED = "node_succeeded"
    NODE_FAILED = "node_failed"
    NODE_RETRYING = "node_retrying"
    NODE_SKIPPED = "node_skipped"
    NODE_CANCELLED = "node_cancelled"
    NODE_EVENT = "node_event"


class ErrorInfo(BaseModel):
    code: str
    message: str
    node_id: str | None = None
    retryable: bool = False


class TraceEvent(BaseModel):
    """One correlated, redacted trace record. ``seq`` is monotonically increasing per run."""

    run_id: str
    project_id: str
    seq: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    status: str | None = None
    node_id: str | None = None
    node_type: str | None = None
    invocation_id: str | None = None
    attempt: int | None = None
    retry_count: int | None = None
    duration_ms: float | None = None
    inputs_summary: Any = None
    outputs_summary: Any = None
    usage: ModelUsage | None = None
    error: ErrorInfo | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RunObserver(Protocol):
    async def on_event(self, event: TraceEvent) -> None: ...

    async def on_node_outputs(
        self, run_id: str, node_id: str, invocation_id: str, outputs: Mapping[str, Any]
    ) -> None: ...


class InMemoryObserver:
    """Collects events and outputs in memory (tests, CLI)."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self.outputs: dict[str, dict[str, Any]] = {}

    async def on_event(self, event: TraceEvent) -> None:
        self.events.append(event)

    async def on_node_outputs(self, run_id: str, node_id: str, invocation_id: str, outputs: Mapping[str, Any]) -> None:
        self.outputs[node_id] = dict(outputs)

    def types(self, node_id: str | None = None) -> list[str]:
        return [e.event_type.value for e in self.events if node_id is None or e.node_id == node_id]

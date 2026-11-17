"""First-party graph execution engine.

Semantics (see docs/architecture/runtime.md for the full description):

* A node becomes *ready* when every upstream node (data or control) is in a terminal state.
* If a node has incoming control edges, it runs only if at least one of them comes from a
  succeeded node that activated that branch (OR-join); otherwise it is skipped.
* A required input whose producers were all skipped causes the node to be skipped (skip
  propagation). ``multiple``-cardinality inputs receive values in edge-ID order (explicit join).
* Ready nodes run concurrently up to ``max_concurrency``.
* Failure is fail-fast: the first node failure (after retries) cancels in-flight nodes and fails
  the run.
* Retries apply only to retryable errors, within ``max_attempts``, with exponential backoff.
  Timeouts are retried only for nodes declared idempotent.
* Cancellation is cooperative through ``CancelToken`` and is reflected in events.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agenticmind_node_sdk import (
    Cardinality,
    NodeContext,
    NodeError,
    NodeResult,
    is_json_value,
    resolve_port_type,
    value_matches_type,
)

from .artifacts import ArtifactStore
from .events import ErrorInfo, EventType, NodeStatus, RunObserver, RunStatus, TraceEvent
from .plan import ExecutionPlan, PlannedNode
from .redaction import summarize
from .secrets import SecretResolver


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class CancelToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


@dataclass
class RunResult:
    run_id: str
    status: RunStatus
    outputs: dict[str, Any]
    node_status: dict[str, NodeStatus]
    error: ErrorInfo | None
    started_at: datetime
    finished_at: datetime


@dataclass
class _Outcome:
    ok: bool
    outputs: dict[str, Any] = field(default_factory=dict)
    branches: frozenset[str] = frozenset()
    run_outputs: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None


class Engine:
    def __init__(
        self,
        *,
        observer: RunObserver | None = None,
        secrets: SecretResolver | None = None,
        max_concurrency: int = 8,
        retry_backoff_seconds: float = 0.2,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.observer = observer
        self.secrets = secrets
        self.max_concurrency = max_concurrency
        self.retry_backoff_seconds = retry_backoff_seconds
        self.artifacts = artifacts

    async def run(
        self,
        plan: ExecutionPlan,
        *,
        run_id: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        cancel: CancelToken | None = None,
    ) -> RunResult:
        execution = _RunExecution(self, plan, run_id or new_id("run"), dict(inputs or {}), cancel or CancelToken())
        return await execution.execute()


class _RunExecution:
    def __init__(
        self, engine: Engine, plan: ExecutionPlan, run_id: str, inputs: dict[str, Any], cancel: CancelToken
    ) -> None:
        self.engine = engine
        self.plan = plan
        self.run_id = run_id
        self.inputs = inputs
        self.cancel = cancel
        self.seq = 0
        self.status: dict[str, NodeStatus] = {nid: NodeStatus.PENDING for nid in plan.order}
        self.outputs: dict[str, dict[str, Any]] = {}
        self.activated: dict[str, frozenset[str]] = {}
        self.run_outputs: dict[str, Any] = {}
        self.secret_values: list[str] = self._collect_secret_values()

    # -- helpers -----------------------------------------------------------------------------
    def _collect_secret_values(self) -> list[str]:
        if self.engine.secrets is None:
            return []
        names = {n for pn in self.plan.nodes.values() for n in pn.allowed_secrets}
        return [v for v in (self.engine.secrets.get(n) for n in sorted(names)) if v]

    def _summary(self, value: Any) -> Any:
        return summarize(value, secrets=self.secret_values)

    def _scrub(self, text: str) -> str:
        for s in self.secret_values:
            text = text.replace(s, "***")
        return text

    async def emit(self, event_type: EventType, **fields: Any) -> None:
        self.seq += 1
        event = TraceEvent(
            run_id=self.run_id, project_id=self.plan.project_id, seq=self.seq, event_type=event_type, **fields
        )
        if self.engine.observer is not None:
            await self.engine.observer.on_event(event)

    # -- main loop -----------------------------------------------------------------------------
    async def execute(self) -> RunResult:
        started_at = datetime.now(UTC)
        t0 = time.perf_counter()
        await self.emit(
            EventType.RUN_STARTED,
            status=RunStatus.RUNNING.value,
            inputs_summary=self._summary(self.inputs),
            data={"node_count": len(self.plan.nodes), "order": list(self.plan.order)},
        )
        pending = list(self.plan.order)
        running: dict[asyncio.Task[_Outcome], str] = {}
        cancel_task = asyncio.create_task(self.cancel.wait())
        failure: ErrorInfo | None = None
        cancelled = False
        try:
            while True:
                await self._schedule(pending, running)
                if not running:
                    break
                done, _ = await asyncio.wait({*running, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
                if cancel_task in done:
                    cancelled = True
                    break
                for task in done:
                    node_id = running.pop(task)  # type: ignore[arg-type]
                    outcome = task.result()  # type: ignore[union-attr]
                    if outcome.ok:
                        self.status[node_id] = NodeStatus.SUCCEEDED
                        self.outputs[node_id] = outcome.outputs
                        self.activated[node_id] = outcome.branches
                        self.run_outputs.update(outcome.run_outputs)
                    else:
                        self.status[node_id] = NodeStatus.FAILED
                        failure = failure or outcome.error
                if failure is not None:
                    break
        finally:
            cancel_task.cancel()
            if running:
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)
                for node_id in running.values():
                    self.status[node_id] = NodeStatus.CANCELLED
                    await self.emit(
                        EventType.NODE_CANCELLED,
                        node_id=node_id,
                        node_type=self.plan.nodes[node_id].node.type,
                        status=NodeStatus.CANCELLED.value,
                        data={"reason": "run_cancelled" if cancelled else "run_failed"},
                    )
            for node_id in pending:
                self.status[node_id] = NodeStatus.CANCELLED

        duration_ms = (time.perf_counter() - t0) * 1000
        node_status = {k: v.value for k, v in self.status.items()}
        if cancelled:
            status = RunStatus.CANCELLED
            await self.emit(EventType.RUN_CANCELLED, status=status.value, duration_ms=duration_ms,
                            data={"node_status": node_status})
        elif failure is not None:
            status = RunStatus.FAILED
            await self.emit(EventType.RUN_FAILED, status=status.value, duration_ms=duration_ms, error=failure,
                            data={"node_status": node_status})
        else:
            status = RunStatus.SUCCEEDED
            await self.emit(
                EventType.RUN_SUCCEEDED,
                status=status.value,
                duration_ms=duration_ms,
                outputs_summary=self._summary(self.run_outputs),
                data={"node_status": node_status},
            )
        return RunResult(
            run_id=self.run_id,
            status=status,
            outputs=dict(self.run_outputs),
            node_status=dict(self.status),
            error=failure,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    async def _schedule(self, pending: list[str], running: dict[asyncio.Task[_Outcome], str]) -> None:
        progressed = True
        while progressed:
            progressed = False
            for node_id in list(pending):
                if len(running) >= self.engine.max_concurrency:
                    return
                pn = self.plan.nodes[node_id]
                if not all(self.status[u].terminal for u in pn.upstream):
                    continue
                pending.remove(node_id)
                prepared = self._prepare(pn)
                if isinstance(prepared, str):
                    self.status[node_id] = NodeStatus.SKIPPED
                    await self.emit(
                        EventType.NODE_SKIPPED,
                        node_id=node_id,
                        node_type=pn.node.type,
                        status=NodeStatus.SKIPPED.value,
                        data={"reason": prepared},
                    )
                    progressed = True
                    continue
                self.status[node_id] = NodeStatus.RUNNING
                running[asyncio.create_task(self._invoke(pn, prepared))] = node_id

    def _prepare(self, pn: PlannedNode) -> dict[str, Any] | str:
        """Resolve inputs, or return a skip reason."""
        if pn.control_in:
            active = any(
                self.status[e.source.node] == NodeStatus.SUCCEEDED
                and e.source.port in self.activated.get(e.source.node, frozenset())
                for e in pn.control_in
            )
            if not active:
                return "control_not_activated"
        values: dict[str, Any] = {}
        for port in pn.entry.definition.manifest.inputs:
            edges = [e for e in pn.data_in if e.target.port == port.name]
            vals = [
                self.outputs[e.source.node][e.source.port]
                for e in edges
                if self.status[e.source.node] == NodeStatus.SUCCEEDED
            ]
            if port.cardinality == Cardinality.MULTIPLE:
                if vals or not port.required:
                    if edges:
                        values[port.name] = vals
                else:
                    return f"upstream_skipped:{port.name}"
            elif vals:
                values[port.name] = vals[0]
            elif port.required:
                return f"upstream_skipped:{port.name}"
        return values

    def _check_result(self, pn: PlannedNode, result: NodeResult) -> frozenset[str]:
        manifest = pn.entry.definition.manifest
        if not isinstance(result, NodeResult):
            raise NodeError("NODE_INVALID_RESULT", "Handler must return NodeResult")
        declared = {p.name for p in manifest.outputs}
        extra = set(result.outputs) - declared
        if extra:
            raise NodeError("NODE_OUTPUT_UNDECLARED", f"Undeclared outputs: {', '.join(sorted(extra))}")
        for port in manifest.outputs:
            if port.name not in result.outputs:
                raise NodeError("NODE_OUTPUT_MISSING", f"Missing output '{port.name}'")
            ptype = resolve_port_type(port, pn.node.config)
            if not value_matches_type(result.outputs[port.name], ptype):
                raise NodeError(
                    "NODE_OUTPUT_TYPE_MISMATCH", f"Output '{port.name}' does not match type {ptype.value}"
                )
        if not is_json_value(dict(result.run_outputs)):
            raise NodeError("NODE_OUTPUT_TYPE_MISMATCH", "Run outputs must be JSON-serialisable")
        branches = frozenset(manifest.branches) if result.branches is None else result.branches
        unknown = branches - set(manifest.branches)
        if unknown:
            raise NodeError("NODE_INVALID_BRANCH", f"Unknown branches: {', '.join(sorted(unknown))}")
        return branches

    async def _invoke(self, pn: PlannedNode, inputs: dict[str, Any]) -> _Outcome:
        manifest = pn.entry.definition.manifest
        invocation_id = new_id("inv")
        inputs_summary = self._summary(inputs)
        common = {"node_id": pn.id, "node_type": pn.node.type, "invocation_id": invocation_id}
        artifacts = (
            self.engine.artifacts.scoped(self.plan.project_id)
            if self.engine.artifacts is not None and manifest.permissions.artifacts
            else None
        )

        async def emit_custom(event_type: str, data: Mapping[str, Any]) -> None:
            await self.emit(EventType.NODE_EVENT, **common, data={"type": event_type, **self._summary(dict(data))})

        for attempt in range(1, pn.max_attempts + 1):
            t0 = time.perf_counter()
            await self.emit(
                EventType.NODE_STARTED,
                **common,
                attempt=attempt,
                retry_count=attempt - 1,
                status=NodeStatus.RUNNING.value,
                inputs_summary=inputs_summary,
            )
            ctx = NodeContext(
                run_id=self.run_id,
                project_id=self.plan.project_id,
                node_id=pn.id,
                invocation_id=invocation_id,
                attempt=attempt,
                run_input=self.inputs,
                allowed_secrets=pn.allowed_secrets,
                _secret_lookup=self.engine.secrets.get if self.engine.secrets else None,
                _emitter=emit_custom,
                _artifacts=artifacts,
            )
            try:
                result = await asyncio.wait_for(
                    pn.entry.definition.handler(ctx, inputs, pn.config), timeout=pn.timeout_seconds
                )
                branches = self._check_result(pn, result)
            except TimeoutError:
                err = NodeError(
                    "NODE_TIMEOUT",
                    f"Timed out after {pn.timeout_seconds:g}s",
                    retryable=manifest.execution.idempotent,
                )
            except NodeError as exc:
                err = exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - node code is untrusted input to the runtime
                err = NodeError("NODE_UNHANDLED_EXCEPTION", f"{type(exc).__name__}: {exc}")
            else:
                duration_ms = (time.perf_counter() - t0) * 1000
                outputs = dict(result.outputs)
                if self.engine.observer is not None:
                    stored = summarize(
                        outputs, secrets=self.secret_values, max_string=200_000, max_items=10_000, max_depth=32
                    )
                    await self.engine.observer.on_node_outputs(self.run_id, pn.id, invocation_id, stored)
                await self.emit(
                    EventType.NODE_SUCCEEDED,
                    **common,
                    attempt=attempt,
                    retry_count=attempt - 1,
                    status=NodeStatus.SUCCEEDED.value,
                    duration_ms=duration_ms,
                    outputs_summary=self._summary(outputs),
                    usage=result.usage,
                    data={"branches": sorted(branches)},
                )
                return _Outcome(ok=True, outputs=outputs, branches=branches, run_outputs=dict(result.run_outputs))

            duration_ms = (time.perf_counter() - t0) * 1000
            info = ErrorInfo(code=err.code, message=self._scrub(err.message), node_id=pn.id, retryable=err.retryable)
            if err.retryable and attempt < pn.max_attempts:
                backoff = self.engine.retry_backoff_seconds * (2 ** (attempt - 1))
                await self.emit(
                    EventType.NODE_RETRYING,
                    **common,
                    attempt=attempt,
                    retry_count=attempt - 1,
                    status=NodeStatus.RUNNING.value,
                    duration_ms=duration_ms,
                    error=info,
                    data={"next_attempt": attempt + 1, "backoff_seconds": backoff},
                )
                await asyncio.sleep(backoff)
                continue
            await self.emit(
                EventType.NODE_FAILED,
                **common,
                attempt=attempt,
                retry_count=attempt - 1,
                status=NodeStatus.FAILED.value,
                duration_ms=duration_ms,
                error=info,
            )
            return _Outcome(ok=False, error=info)
        raise AssertionError("unreachable")  # pragma: no cover

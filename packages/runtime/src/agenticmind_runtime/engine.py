"""First-party graph execution engine.

Semantics (see docs/architecture/runtime.md for the full description):

* A node becomes *ready* when every upstream node (data or control) is in a terminal state.
* If a node has incoming control edges, it runs only if at least one of them (``control_join="any"``,
  the default) or all of them (``control_join="all"``, e.g. Join) come from a succeeded node that
  activated that branch; otherwise it is skipped.
* A required input whose producers were all skipped causes the node to be skipped (skip
  propagation). ``multiple``-cardinality inputs receive values in edge-ID order (explicit join).
* Ready nodes run concurrently up to ``max_concurrency`` per scope.
* Failure is fail-fast: the first node failure (after retries) cancels in-flight nodes and fails
  the run (or, inside a loop body, the iteration).
* Retries apply only to retryable errors, within ``max_attempts``, with exponential backoff.
  Timeouts are retried only for nodes declared idempotent.
* Loops: a loop end executes its body (``ExecutionPlan.regions``) once per iteration, each in a fresh
  scope seeded with the loop start's ``item``/``index``/``state``; events carry ``data.iteration``.
* Cancellation is cooperative through ``CancelToken`` and is reflected in events.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agenticmind_node_sdk import (
    Cardinality,
    NodeContext,
    NodeError,
    NodeResult,
    effective_branches,
    is_json_value,
    resolve_port_type,
    value_matches_type,
)

from .artifacts import ArtifactStore
from .events import ErrorInfo, EventType, NodeStatus, RunObserver, RunStatus, TraceEvent
from .loops import SCOPE_PORT
from .memory import MemoryStore
from .plan import ExecutionPlan, LoopRegion, PlannedNode
from .redaction import summarize
from .secrets import SecretResolver

#: Hard ceiling on iterations of any single loop, whatever its config says.
MAX_LOOP_ITERATIONS = 10_000


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


@dataclass
class _Scope:
    """Execution state of one scope: the whole run, or one loop iteration."""

    status: dict[str, NodeStatus]
    outputs: dict[str, dict[str, Any]]
    activated: dict[str, frozenset[str]]
    run_outputs: dict[str, Any]
    iteration: tuple[int, ...] = ()


class Engine:
    def __init__(
        self,
        *,
        observer: RunObserver | None = None,
        secrets: SecretResolver | None = None,
        max_concurrency: int = 8,
        retry_backoff_seconds: float = 0.2,
        artifacts: ArtifactStore | None = None,
        data_dir: Path | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.observer = observer
        self.secrets = secrets
        self.max_concurrency = max_concurrency
        self.retry_backoff_seconds = retry_backoff_seconds
        self.artifacts = artifacts
        self.data_dir = data_dir
        self.memory = memory

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

    @staticmethod
    def _iter_data(scope: _Scope, data: dict[str, Any] | None = None) -> dict[str, Any]:
        out = dict(data or {})
        if scope.iteration:
            out["iteration"] = list(scope.iteration)
        return out

    # -- run ------------------------------------------------------------------------------------
    async def execute(self) -> RunResult:
        started_at = datetime.now(UTC)
        t0 = time.perf_counter()
        await self.emit(
            EventType.RUN_STARTED,
            status=RunStatus.RUNNING.value,
            inputs_summary=self._summary(self.inputs),
            data={
                "node_count": len(self.plan.nodes),
                "order": list(self.plan.order),
                "targets": list(self.plan.targets),
            },
        )
        root = _Scope(
            status={nid: NodeStatus.PENDING for nid in self.plan.nodes},
            outputs={},
            activated={},
            run_outputs={},
        )
        failure, cancelled = await self._drive(root, list(self.plan.order))

        duration_ms = (time.perf_counter() - t0) * 1000
        node_status = {k: v.value for k, v in root.status.items()}
        if cancelled:
            status = RunStatus.CANCELLED
            await self.emit(
                EventType.RUN_CANCELLED, status=status.value, duration_ms=duration_ms, data={"node_status": node_status}
            )
        elif failure is not None:
            status = RunStatus.FAILED
            await self.emit(
                EventType.RUN_FAILED,
                status=status.value,
                duration_ms=duration_ms,
                error=failure,
                data={"node_status": node_status},
            )
        else:
            status = RunStatus.SUCCEEDED
            await self.emit(
                EventType.RUN_SUCCEEDED,
                status=status.value,
                duration_ms=duration_ms,
                outputs_summary=self._summary(root.run_outputs),
                data={"node_status": node_status},
            )
        return RunResult(
            run_id=self.run_id,
            status=status,
            outputs=dict(root.run_outputs),
            node_status=dict(root.status),
            error=failure,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    async def _drive(self, scope: _Scope, order: list[str]) -> tuple[ErrorInfo | None, bool]:
        """Run ``order`` in ``scope`` to completion. Returns (first failure, cancelled)."""
        pending = list(order)
        running: dict[asyncio.Task[_Outcome], str] = {}
        cancel_task = asyncio.create_task(self.cancel.wait())
        failure: ErrorInfo | None = None
        cancelled = False
        try:
            while True:
                await self._schedule(scope, pending, running)
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
                        scope.status[node_id] = NodeStatus.SUCCEEDED
                        scope.outputs[node_id] = outcome.outputs
                        scope.activated[node_id] = outcome.branches
                        scope.run_outputs.update(outcome.run_outputs)
                    else:
                        scope.status[node_id] = NodeStatus.FAILED
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
                    scope.status[node_id] = NodeStatus.CANCELLED
                    await self.emit(
                        EventType.NODE_CANCELLED,
                        node_id=node_id,
                        node_type=self.plan.nodes[node_id].node.type,
                        status=NodeStatus.CANCELLED.value,
                        data=self._iter_data(scope, {"reason": "run_cancelled" if cancelled else "run_failed"}),
                    )
            for node_id in pending:
                scope.status[node_id] = NodeStatus.CANCELLED
        return failure, cancelled

    async def _schedule(self, scope: _Scope, pending: list[str], running: dict[asyncio.Task[_Outcome], str]) -> None:
        progressed = True
        while progressed:
            progressed = False
            for node_id in list(pending):
                if len(running) >= self.engine.max_concurrency:
                    return
                pn = self.plan.nodes[node_id]
                if not all(scope.status[u].terminal for u in pn.upstream):
                    continue
                pending.remove(node_id)
                region = self.plan.regions.get(node_id)
                if region is not None:
                    prepared: dict[str, Any] | str = (
                        {} if scope.status[region.start] == NodeStatus.SUCCEEDED else "loop_not_started"
                    )
                else:
                    prepared = self._prepare(pn, scope)
                if isinstance(prepared, str):
                    scope.status[node_id] = NodeStatus.SKIPPED
                    if region is not None:
                        for n in region.body:
                            scope.status[n] = NodeStatus.SKIPPED
                    await self.emit(
                        EventType.NODE_SKIPPED,
                        node_id=node_id,
                        node_type=pn.node.type,
                        status=NodeStatus.SKIPPED.value,
                        data=self._iter_data(scope, {"reason": prepared}),
                    )
                    progressed = True
                    continue
                scope.status[node_id] = NodeStatus.RUNNING
                coro = self._run_loop(pn, region, scope) if region is not None else self._invoke(pn, prepared, scope)
                running[asyncio.create_task(coro)] = node_id

    def _prepare(self, pn: PlannedNode, scope: _Scope) -> dict[str, Any] | str:
        """Resolve inputs, or return a skip reason."""
        if pn.control_in:
            hits = [
                scope.status[e.source.node] == NodeStatus.SUCCEEDED
                and e.source.port in scope.activated.get(e.source.node, frozenset())
                for e in pn.control_in
            ]
            join_all = pn.entry.definition.manifest.control_join == "all"
            if not (all(hits) if join_all else any(hits)):
                return "control_not_activated"
        values: dict[str, Any] = {}
        for port in pn.entry.definition.manifest.inputs:
            edges = [e for e in pn.data_in if e.target.port == port.name]
            vals = [
                scope.outputs[e.source.node][e.source.port]
                for e in edges
                if scope.status[e.source.node] == NodeStatus.SUCCEEDED
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
            if manifest.runtime_role == "loop_start" and port.name != SCOPE_PORT:
                continue  # per-iteration values are seeded (and validated) by the loop start itself
            ptype = resolve_port_type(port, pn.node.config)
            if not value_matches_type(result.outputs[port.name], ptype):
                raise NodeError("NODE_OUTPUT_TYPE_MISMATCH", f"Output '{port.name}' does not match type {ptype.value}")
        if not is_json_value(dict(result.run_outputs)):
            raise NodeError("NODE_OUTPUT_TYPE_MISMATCH", "Run outputs must be JSON-serialisable")
        allowed = effective_branches(manifest, pn.node.config)
        branches = frozenset(allowed) if result.branches is None else result.branches
        unknown = branches - set(allowed)
        if unknown:
            raise NodeError("NODE_INVALID_BRANCH", f"Unknown branches: {', '.join(sorted(unknown))}")
        return branches

    async def _invoke(self, pn: PlannedNode, inputs: dict[str, Any], scope: _Scope) -> _Outcome:
        manifest = pn.entry.definition.manifest
        invocation_id = new_id("inv")
        inputs_summary = self._summary(inputs)
        common = {"node_id": pn.id, "node_type": pn.node.type, "invocation_id": invocation_id}
        artifacts = (
            self.engine.artifacts.scoped(self.plan.project_id)
            if self.engine.artifacts is not None and manifest.permissions.artifacts
            else None
        )
        memory = (
            self.engine.memory.scoped(self.plan.project_id)
            if self.engine.memory is not None and manifest.permissions.memory
            else None
        )

        async def emit_custom(event_type: str, data: Mapping[str, Any]) -> None:
            await self.emit(
                EventType.NODE_EVENT,
                **common,
                data=self._iter_data(scope, {"type": event_type, **self._summary(dict(data))}),
            )

        for attempt in range(1, pn.max_attempts + 1):
            t0 = time.perf_counter()
            await self.emit(
                EventType.NODE_STARTED,
                **common,
                attempt=attempt,
                retry_count=attempt - 1,
                status=NodeStatus.RUNNING.value,
                inputs_summary=inputs_summary,
                data=self._iter_data(scope),
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
                _storage_root=self.engine.data_dir if manifest.permissions.filesystem else None,
                _memory=memory,
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
                await self._store_outputs(pn.id, invocation_id, outputs)
                await self.emit(
                    EventType.NODE_SUCCEEDED,
                    **common,
                    attempt=attempt,
                    retry_count=attempt - 1,
                    status=NodeStatus.SUCCEEDED.value,
                    duration_ms=duration_ms,
                    outputs_summary=self._summary(outputs),
                    usage=result.usage,
                    data=self._iter_data(scope, {"branches": sorted(branches)}),
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
                    data=self._iter_data(scope, {"next_attempt": attempt + 1, "backoff_seconds": backoff}),
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
                data=self._iter_data(scope),
            )
            return _Outcome(ok=False, error=info)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _store_outputs(self, node_id: str, invocation_id: str, outputs: dict[str, Any]) -> None:
        if self.engine.observer is not None:
            stored = summarize(outputs, secrets=self.secret_values, max_string=200_000, max_items=10_000, max_depth=32)
            await self.engine.observer.on_node_outputs(self.run_id, node_id, invocation_id, stored)

    # -- loops ----------------------------------------------------------------------------------
    def _child_scope(self, parent: _Scope, region: LoopRegion, index: int, item: Any, state: Any) -> _Scope:
        status = dict(parent.status)
        outputs = dict(parent.outputs)
        activated = dict(parent.activated)
        for n in region.body:
            status[n] = NodeStatus.PENDING
            outputs.pop(n, None)
            activated.pop(n, None)
        outputs[region.start] = {**parent.outputs[region.start], "item": item, "index": index, "state": state}
        return _Scope(status, outputs, activated, run_outputs={}, iteration=(*parent.iteration, index))

    def _end_inputs(self, end: PlannedNode, scope: _Scope) -> dict[str, Any]:
        """Values arriving at the loop end's body inputs in one iteration (None when skipped)."""
        values: dict[str, Any] = {}
        for e in end.data_in:
            if e.target.port == SCOPE_PORT or e.target.port in values:
                continue
            if scope.status.get(e.source.node) == NodeStatus.SUCCEEDED:
                values[e.target.port] = scope.outputs[e.source.node][e.source.port]
        return values

    async def _run_loop(self, end: PlannedNode, region: LoopRegion, scope: _Scope) -> _Outcome:
        spec = scope.outputs[region.start][SCOPE_PORT]
        mode = spec.get("mode", "for_each")
        invocation_id = new_id("inv")
        common = {"node_id": end.id, "node_type": end.node.type, "invocation_id": invocation_id}
        t0 = time.perf_counter()
        await self.emit(
            EventType.NODE_STARTED,
            **common,
            attempt=1,
            retry_count=0,
            status=NodeStatus.RUNNING.value,
            data=self._iter_data(scope, {"loop": mode}),
        )
        last_child: _Scope | None = None
        errors: list[dict[str, Any]] = []
        stop_reason = "items_exhausted"

        async def iteration(index: int, item: Any, state: Any) -> tuple[_Scope, ErrorInfo | None]:
            child = self._child_scope(scope, region, index, item, state)
            failure, cancelled = await self._drive(child, list(region.order))
            if cancelled:
                raise asyncio.CancelledError
            await self.emit(
                EventType.NODE_EVENT,
                **common,
                data=self._iter_data(child, {"type": "loop_iteration", "ok": failure is None}),
            )
            return child, failure

        async def fail(error: ErrorInfo) -> _Outcome:
            await self.emit(
                EventType.NODE_FAILED,
                **common,
                attempt=1,
                retry_count=0,
                status=NodeStatus.FAILED.value,
                duration_ms=(time.perf_counter() - t0) * 1000,
                error=ErrorInfo(code=error.code, message=error.message, node_id=end.id),
                data=self._iter_data(scope),
            )
            return _Outcome(ok=False, error=error)

        def in_iteration(index: int, error: ErrorInfo) -> ErrorInfo:
            return ErrorInfo(
                code=error.code,
                message=f"Loop '{end.id}', iteration {index}: {error.message}",
                node_id=error.node_id,
                retryable=False,
            )

        if mode == "for_each":
            items = list(spec.get("items") or [])[:MAX_LOOP_ITERATIONS]
            results: list[Any] = [None] * len(items)
            concurrency = max(1, int(spec.get("concurrency", 1)))
            skip_errors = spec.get("on_item_error") == "skip"
            semaphore = asyncio.Semaphore(concurrency)

            async def bounded(i: int) -> tuple[int, _Scope, ErrorInfo | None]:
                async with semaphore:
                    child, failure = await iteration(i, items[i], None)
                    return i, child, failure

            tasks = [asyncio.create_task(bounded(i)) for i in range(len(items))]
            try:
                for next_done in asyncio.as_completed(tasks):
                    i, child, failure = await next_done
                    if failure is not None:
                        if not skip_errors:
                            return await fail(in_iteration(i, failure))
                        errors.append({"index": i, "code": failure.code, "message": failure.message})
                        continue
                    results[i] = self._end_inputs(end, child).get("value")
                    if last_child is None or i >= max(last_child.iteration[-1], 0):
                        last_child = child
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            last_value = next((r for r in reversed(results) if r is not None), None)
        else:  # repeat
            state = spec.get("initial")
            max_iterations = min(int(spec.get("max_iterations", 5)), MAX_LOOP_ITERATIONS)
            results = []
            stop_reason = "max_iterations"
            for i in range(max_iterations):
                child, failure = await iteration(i, state, state)
                if failure is not None:
                    return await fail(in_iteration(i, failure))
                last_child = child
                values = self._end_inputs(end, child)
                value = values.get("value")
                results.append(value)
                if value is not None:
                    state = value
                if values.get("continue") is False:
                    stop_reason = "condition"
                    break
            last_value = results[-1] if results else None

        if not is_json_value(results):
            return await fail(
                ErrorInfo(
                    code="LOOP_RESULT_NOT_JSON",
                    message=f"Loop '{end.id}' produced values that are not JSON-serialisable",
                    node_id=end.id,
                )
            )
        for n in region.body:  # reflect the final iteration in the enclosing scope's status
            scope.status[n] = last_child.status.get(n, NodeStatus.SKIPPED) if last_child else NodeStatus.SKIPPED
        report = {
            "mode": mode,
            "iterations": len(results),
            "stop_reason": stop_reason,
            "errors": errors,
        }
        outputs = {"results": results, "last": last_value, "report": report}
        await self._store_outputs(end.id, invocation_id, outputs)
        await self.emit(
            EventType.NODE_SUCCEEDED,
            **common,
            attempt=1,
            retry_count=0,
            status=NodeStatus.SUCCEEDED.value,
            duration_ms=(time.perf_counter() - t0) * 1000,
            outputs_summary=self._summary(outputs),
            data=self._iter_data(scope, {"branches": ["done"], "loop": report}),
        )
        return _Outcome(ok=True, outputs=outputs, branches=frozenset({"done"}))

"""Durable run orchestration (ADR-0005, ADR-0013, ADR-0039).

Starting or resuming a run writes a **job** to the database; workers execute jobs:

* **Claiming** is one conditional UPDATE per job, so a job never runs twice at once. The worker holds a
  **lease** it renews every few seconds while the run executes.
* **Crash recovery:** if a worker dies (or the server is stopped), its lease expires and any worker claims the
  job again and **resumes from the run's checkpoints** (finished nodes are restored, not re-run). A
  non-idempotent node that was mid-flight is not repeated automatically: the run is marked interrupted and
  needs a manual resume with ``force``. Recoveries per run are capped (``max_run_attempts``).
* **Workers:** one embedded in the API process (default, so a single process behaves as before) and/or any
  number of ``agenticmind-worker`` processes sharing the database.
* **Cancel** works across processes (a flag the owning worker sees on its next heartbeat); **wait** polls the
  database when the run executes elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agenticmind_ir import ProjectDocument, load_project
from agenticmind_registry import NodeRegistry, TrustLevel
from agenticmind_runtime import (
    ArtifactStore,
    CancelToken,
    Engine,
    ExecutionPlan,
    MemoryStore,
    NodeCheckpoint,
    PlanError,
    ResumeState,
    RunStatus,
    SecretResolver,
    TraceEvent,
    compile_plan,
    new_id,
)

from .db import ProjectRevisionRow, RunJobRow
from .store import Store

log = logging.getLogger("agenticmind.runs")
_FINISHED = ("node_succeeded", "node_failed", "node_skipped", "node_cancelled", "node_waiting")


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class DbObserver:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def on_event(self, event: TraceEvent) -> None:
        self.store.add_event(event)

    async def on_checkpoint(self, run_id: str, checkpoint: NodeCheckpoint) -> None:
        self.store.put_checkpoint(run_id, checkpoint)

    async def on_node_outputs(self, run_id: str, node_id: str, invocation_id: str, outputs: Mapping[str, Any]) -> None:
        self.store.put_node_outputs(run_id, node_id, invocation_id, outputs)


class RunManager:
    def __init__(
        self,
        store: Store,
        registry: NodeRegistry,
        secrets: SecretResolver,
        *,
        in_process_trust: frozenset[TrustLevel],
        max_concurrency: int = 8,
        artifacts: ArtifactStore | None = None,
        data_dir: Path | None = None,
        memory: MemoryStore | None = None,
        worker_id: str | None = None,
        lease_seconds: float = 30.0,
        max_run_attempts: int = 3,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.registry = registry
        self.secrets = secrets
        self.in_process_trust = in_process_trust
        self.max_concurrency = max_concurrency
        self.artifacts = artifacts
        self.data_dir = data_dir
        self.memory = memory
        self.worker_id = worker_id or default_worker_id()
        self.lease_seconds = lease_seconds
        self.max_run_attempts = max_run_attempts
        self.poll_seconds = poll_seconds
        self._active: dict[str, tuple[asyncio.Task[None], CancelToken]] = {}
        #: Called with the run id after a run is recorded as finished (metrics, trace export).
        self.finish_hooks: list[Callable[[str], Awaitable[None]]] = []
        self._worker: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._stopping = False

    def plan(self, doc: ProjectDocument, targets: Sequence[str] | None = None) -> ExecutionPlan:
        """Raises ``PlanError`` with diagnostics if the graph is not executable."""
        return compile_plan(
            doc, self.registry, secrets=self.secrets, in_process_trust=self.in_process_trust, targets=targets
        )

    # ---- submitting work --------------------------------------------------------------------------
    def start(
        self, project_id: str, revision: ProjectRevisionRow, plan: ExecutionPlan, inputs: Mapping[str, Any]
    ) -> str:
        run_id = new_id("run")
        self.store.create_run(run_id, project_id, revision, inputs, targets=plan.targets)
        self._notify()
        return run_id

    def resume(
        self, run_id: str, plan: ExecutionPlan, inputs: Mapping[str, Any], state: ResumeState, *, force: bool = False
    ) -> None:
        """Queue a resume (a waiting run with responses, or an interrupted run) for a worker."""
        self.store.enqueue_resume(run_id, responses=state.responses, targets=plan.targets, force=force)
        self._notify()

    def _notify(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ---- queries and control ------------------------------------------------------------------------
    def is_active(self, run_id: str) -> bool:
        if run_id in self._active:
            return True
        job = self.store.get_job(run_id)
        return job is not None and job.status in ("queued", "leased")

    def cancel(self, run_id: str) -> bool:
        active = self._active.get(run_id)
        if active is not None:
            active[1].cancel()
            return True
        return self.store.request_cancel(run_id) != ""

    async def wait(self, run_id: str, timeout: float | None = None) -> None:
        """Until the run ends (or waits for approval). Raises ``TimeoutError``."""
        active = self._active.get(run_id)
        if active is not None:
            await asyncio.wait_for(asyncio.shield(active[0]), timeout)
            return

        async def poll() -> None:
            while True:
                row = self.store.get_run(run_id)
                if row is None or RunStatus(row.status).terminal:
                    return
                if row.status == RunStatus.WAITING.value and not self.is_active(run_id):
                    return
                local = self._active.get(run_id)
                if local is not None:
                    await asyncio.shield(local[0])
                    return
                await asyncio.sleep(0.2)

        await asyncio.wait_for(poll(), timeout)

    def unfinished_side_effects(self, run_id: str, plan: ExecutionPlan) -> list[str]:
        """Non-idempotent nodes that started but never finished before an interruption."""
        started: set[str] = set()
        finished: set[str] = set()
        for e in self.store.list_events(run_id, after=0, limit=100_000):
            if not e.node_id:
                continue
            if e.event_type == "node_started":
                started.add(e.node_id)
            elif e.event_type in _FINISHED:
                finished.add(e.node_id)
        restored = {c.node_id for c in self.store.list_checkpoints(run_id) if c.restorable}
        return sorted(
            n
            for n in started - finished - restored
            if n in plan.nodes and not plan.nodes[n].entry.definition.manifest.execution.idempotent
        )

    # ---- the worker -------------------------------------------------------------------------------
    async def start_worker(self) -> None:
        if self._worker is None:
            self._stopping = False
            self._wake = asyncio.Event()
            self._worker = asyncio.create_task(self._worker_loop(), name=f"worker:{self.worker_id}")

    async def _worker_loop(self) -> None:
        assert self._wake is not None
        while not self._stopping:
            try:
                free = self.max_concurrency - len(self._active)
                jobs = await asyncio.to_thread(self.store.claim_jobs, self.worker_id, self.lease_seconds, free)
                for job in jobs:
                    self._launch(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive database hiccups
                log.exception("worker loop error")
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.poll_seconds)

    def _launch(self, job: RunJobRow) -> None:
        token = CancelToken()
        task = asyncio.create_task(self._run_job(job, token), name=f"run:{job.run_id}")
        self._active[job.run_id] = (task, token)
        task.add_done_callback(lambda _t, rid=job.run_id: self._done(rid))

    def _done(self, run_id: str) -> None:
        self._active.pop(run_id, None)
        self._notify()  # capacity freed: look for more work

    async def _run_job(self, job: RunJobRow, token: CancelToken) -> None:
        run_id = job.run_id
        heartbeat = asyncio.create_task(self._heartbeat(run_id, token))
        released = False
        try:
            row = self.store.get_run(run_id)
            if row is None or RunStatus(row.status).terminal:
                return
            try:
                plan = self.plan(load_project(row.ir_snapshot), targets=job.targets or None)
            except PlanError as exc:
                self.store.fail_run(run_id, "PLAN_INVALID", str(exc))
                return
            resume: ResumeState | None = None
            recovering = job.attempts > 1
            if job.kind == "resume" or recovering:
                if recovering and job.attempts > self.max_run_attempts:
                    self.store.fail_run(
                        run_id, "RUN_RECOVERY_LIMIT", f"The run was interrupted {job.attempts - 1} times; giving up"
                    )
                    return
                if recovering and not job.force:
                    unsafe = self.unfinished_side_effects(run_id, plan)
                    if unsafe:
                        self.store.fail_run(
                            run_id,
                            "RUN_INTERRUPTED",
                            f"Interrupted while {', '.join(unsafe)} (side effects) ran; resume with "
                            "force=true to run them again",
                        )
                        return
                resume = ResumeState(
                    checkpoints=self.store.list_checkpoints(run_id),
                    responses=dict(job.responses or {}),
                    last_seq=self.store.last_seq(run_id),
                )
                if recovering:
                    log.warning("run %s: recovering on %s (attempt %d)", run_id, self.worker_id, job.attempts)
            await self._execute(run_id, plan, dict(row.inputs), token, resume)
        except asyncio.CancelledError:
            # worker shutdown: hand the job back; another worker (or this one after a restart) resumes it
            self.store.release_job(run_id, self.worker_id)
            released = True
            raise
        finally:
            heartbeat.cancel()
            if not released:
                self.store.finish_job(run_id, self.worker_id)

    async def _heartbeat(self, run_id: str, token: CancelToken) -> None:
        interval = max(self.lease_seconds / 3, 0.2)
        while True:
            await asyncio.sleep(interval)
            try:
                owner, cancel = await asyncio.to_thread(
                    self.store.renew_lease, run_id, self.worker_id, self.lease_seconds
                )
            except Exception:  # noqa: BLE001
                log.exception("lease renewal failed for %s", run_id)
                continue
            if cancel or not owner:  # cancelled from another process, or the lease was lost
                token.cancel()

    async def _execute(
        self,
        run_id: str,
        plan: ExecutionPlan,
        inputs: dict[str, Any],
        token: CancelToken,
        resume: ResumeState | None = None,
    ) -> None:
        try:
            if resume is None:
                self.store.mark_run_started(run_id)
            engine = Engine(
                observer=DbObserver(self.store),
                secrets=self.secrets,
                max_concurrency=self.max_concurrency,
                artifacts=self.artifacts,
                data_dir=self.data_dir,
                memory=self.memory,
            )
            result = await engine.run(plan, run_id=run_id, inputs=inputs, cancel=token, resume=resume)
            self.store.finish_run(result)
            for hook in self.finish_hooks:
                try:
                    await hook(run_id)
                except Exception:  # noqa: BLE001 - observers must never fail a run
                    log.exception("finish hook failed for run %s", run_id)
        except asyncio.CancelledError:
            raise  # shutdown: the job is released and the run resumes elsewhere
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s crashed", run_id)
            self.store.fail_run(run_id, "RUNTIME_INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        """Stop claiming; give running runs a moment, then hand their jobs back for another worker."""
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        tasks = [t for t, _ in self._active.values()]
        if tasks:
            _done, still = await asyncio.wait(tasks, timeout=grace_seconds)
            for t in still:
                t.cancel()
            await asyncio.gather(*still, return_exceptions=True)

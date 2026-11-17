"""In-process run orchestration.

Runs execute as asyncio tasks inside the API process. This is the smallest reversible default
(ADR-0005): the ``RunManager`` interface is what a Redis/queue-backed worker pool will implement
later, together with durable checkpoints for resume.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agenticmind_ir import ProjectDocument
from agenticmind_registry import NodeRegistry, TrustLevel
from agenticmind_runtime import (
    ArtifactStore,
    CancelToken,
    Engine,
    ExecutionPlan,
    MemoryStore,
    NodeCheckpoint,
    ResumeState,
    SecretResolver,
    TraceEvent,
    compile_plan,
    new_id,
)

from .db import ProjectRevisionRow
from .store import Store

log = logging.getLogger("agenticmind.runs")


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
    ) -> None:
        self.store = store
        self.registry = registry
        self.secrets = secrets
        self.in_process_trust = in_process_trust
        self.max_concurrency = max_concurrency
        self.artifacts = artifacts
        self.data_dir = data_dir
        self.memory = memory
        self._active: dict[str, tuple[asyncio.Task[None], CancelToken]] = {}

    def plan(self, doc: ProjectDocument, targets: Sequence[str] | None = None) -> ExecutionPlan:
        """Raises ``PlanError`` with diagnostics if the graph is not executable."""
        return compile_plan(
            doc, self.registry, secrets=self.secrets, in_process_trust=self.in_process_trust, targets=targets
        )

    def start(
        self, project_id: str, revision: ProjectRevisionRow, plan: ExecutionPlan, inputs: Mapping[str, Any]
    ) -> str:
        run_id = new_id("run")
        self.store.create_run(run_id, project_id, revision, inputs)
        token = CancelToken()
        task = asyncio.create_task(self._execute(run_id, plan, dict(inputs), token), name=f"run:{run_id}")
        self._active[run_id] = (task, token)
        task.add_done_callback(lambda _t: self._active.pop(run_id, None))
        return run_id

    def resume(self, run_id: str, plan: ExecutionPlan, inputs: Mapping[str, Any], state: ResumeState) -> None:
        """Continue a run (already claimed via ``Store.claim_for_resume``) from its checkpoints."""
        token = CancelToken()
        task = asyncio.create_task(
            self._execute(run_id, plan, dict(inputs), token, resume=state), name=f"resume:{run_id}"
        )
        self._active[run_id] = (task, token)
        task.add_done_callback(lambda _t: self._active.pop(run_id, None))

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
        except asyncio.CancelledError:
            self.store.fail_run(run_id, "RUN_ABORTED", "Run task was aborted (server shutdown)")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s crashed", run_id)
            self.store.fail_run(run_id, "RUNTIME_INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    def cancel(self, run_id: str) -> bool:
        active = self._active.get(run_id)
        if active is None:
            return False
        active[1].cancel()
        return True

    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

    async def wait(self, run_id: str, timeout: float | None = None) -> None:
        active = self._active.get(run_id)
        if active is not None:
            await asyncio.wait_for(asyncio.shield(active[0]), timeout)

    async def shutdown(self, grace_seconds: float = 5.0) -> None:
        for _task, token in list(self._active.values()):
            token.cancel()
        tasks = [t for t, _ in self._active.values()]
        if tasks:
            _done, still = await asyncio.wait(tasks, timeout=grace_seconds)
            for t in still:
                t.cancel()

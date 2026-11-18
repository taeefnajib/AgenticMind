"""Runs evaluations in the background; every case is a normal, traced run linked to its scored result."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from agenticmind_evaluation import CaseResult, DatasetContent, EvaluationConfig, EvaluationError, run_evaluation
from agenticmind_ir import load_project
from agenticmind_registry import NodeRegistry
from agenticmind_runtime import ArtifactStore, CancelToken, MemoryStore, RunResult, SecretResolver

from .db import DatasetRow, EvalRunRow, ProjectRevisionRow
from .eval_store import EvalStore
from .run_manager import DbObserver
from .store import Store

log = logging.getLogger("agenticmind.api.eval")


class _Hooks:
    def __init__(self, manager: EvaluationManager, eval_id: str, project_id: str, rev: ProjectRevisionRow, order: dict):
        self.m, self.eval_id, self.project_id, self.rev, self.order = manager, eval_id, project_id, rev, order

    def observer(self, run_id: str, case: Any) -> DbObserver:
        return DbObserver(self.m.store)

    def started(self, run_id: str, case: Any) -> None:
        self.m.store.create_run(run_id, self.project_id, self.rev, case.inputs)
        self.m.store.mark_run_started(run_id)

    def finished(self, result: RunResult) -> None:
        self.m.store.finish_run(result)

    def case_done(self, result: CaseResult) -> None:
        self.m.evals.put_case(
            self.eval_id,
            self.order[result.case_id],
            result.case_id,
            result.run_id,
            result.status,
            result.model_dump(mode="json"),
        )
        self.m.evals.increment_done(self.eval_id)


class EvaluationManager:
    def __init__(
        self,
        store: Store,
        evals: EvalStore,
        registry: NodeRegistry,
        secrets: SecretResolver,
        *,
        artifacts: ArtifactStore | None = None,
        data_dir: Path | None = None,
        memory: MemoryStore | None = None,
        max_concurrency: int = 8,
    ) -> None:
        self.store, self.evals, self.registry, self.secrets = store, evals, registry, secrets
        self.engine_kwargs = {
            "artifacts": artifacts,
            "data_dir": data_dir,
            "memory": memory,
            "max_concurrency": max_concurrency,
        }
        self._active: dict[str, tuple[asyncio.Task[None], CancelToken]] = {}

    def start(
        self, project_id: str, rev: ProjectRevisionRow, dataset: DatasetRow, config: EvaluationConfig, label: str
    ) -> EvalRunRow:
        content = DatasetContent.model_validate(dataset.content)
        row = self.evals.create_eval_run(
            project_id=project_id,
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            dataset_hash=dataset.content_hash,
            revision=rev.revision,
            content_hash=rev.content_hash,
            label=label,
            config=config.model_dump(mode="json"),
            total=len(content.cases),
        )
        token = CancelToken()
        task = asyncio.create_task(self._run(row.id, project_id, rev, content, config, token), name=f"eval:{row.id}")
        self._active[row.id] = (task, token)
        task.add_done_callback(lambda _t: self._active.pop(row.id, None))
        return row

    async def _run(
        self,
        eval_id: str,
        project_id: str,
        rev: ProjectRevisionRow,
        content: DatasetContent,
        config: EvaluationConfig,
        token: CancelToken,
    ) -> None:
        self.evals.update_eval_run(eval_id, status="running")
        judge_key = None
        if config.judge and config.judge.api_key and "$secret" in config.judge.api_key:
            judge_key = self.secrets.get(config.judge.api_key["$secret"])
        order = {c.id: i for i, c in enumerate(content.cases)}
        try:
            out = await run_evaluation(
                load_project(rev.document),
                self.registry,
                content,
                config,
                secrets=self.secrets,
                engine_kwargs=self.engine_kwargs,
                hooks=_Hooks(self, eval_id, project_id, rev, order),
                judge_api_key=judge_key,
                cancel=token,
            )
        except EvaluationError as exc:
            self.evals.update_eval_run(
                eval_id,
                status="failed",
                error={"code": "EVAL_INVALID", "message": str(exc), "diagnostics": exc.diagnostics},
            )
            return
        except asyncio.CancelledError:
            self.evals.update_eval_run(eval_id, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("evaluation %s crashed", eval_id)
            self.evals.update_eval_run(
                eval_id,
                status="failed",
                error={"code": "EVAL_INTERNAL_ERROR", "message": f"{type(exc).__name__}: {exc}"},
            )
            return
        status = "cancelled" if token.cancelled else "succeeded"
        self.evals.update_eval_run(eval_id, status=status, summary=out["summary"], setup=out["setup"])

    def cancel(self, eval_id: str) -> bool:
        active = self._active.get(eval_id)
        if active is None:
            return False
        active[1].cancel()
        return True

    def is_active(self, eval_id: str) -> bool:
        return eval_id in self._active

    async def wait(self, eval_id: str, timeout: float | None = None) -> None:
        active = self._active.get(eval_id)
        if active is not None:
            await asyncio.wait_for(asyncio.shield(active[0]), timeout)

    async def shutdown(self) -> None:
        for task, token in list(self._active.values()):
            token.cancel()
            task.cancel()
        if self._active:
            await asyncio.gather(*(t for t, _ in self._active.values()), return_exceptions=True)

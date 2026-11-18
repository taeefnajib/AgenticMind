"""Persistence for the Evaluation Lab: dataset versions, evaluation runs and per-case results (ADR-0017)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker

from .db import DatasetRow, EvalCaseRow, EvalRunRow, utcnow

TERMINAL = ("succeeded", "failed", "cancelled")


class EvalStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory

    # -- datasets ---------------------------------------------------------------------------------
    def create_dataset(
        self, project_id: str, name: str, description: str, content: dict, content_hash: str
    ) -> DatasetRow:
        row = DatasetRow(
            id=f"ds_{uuid.uuid4().hex[:12]}",
            version=1,
            project_id=project_id,
            name=name,
            description=description,
            content=content,
            content_hash=content_hash,
        )
        with self._sf.begin() as s:
            s.add(row)
        return row

    def get_dataset(self, dataset_id: str, version: int | None = None) -> DatasetRow | None:
        with self._sf() as s:
            stmt = select(DatasetRow).where(DatasetRow.id == dataset_id)
            stmt = stmt.where(DatasetRow.version == version) if version else stmt.order_by(DatasetRow.version.desc())
            return s.scalars(stmt.limit(1)).first()

    def new_dataset_version(
        self, dataset_id: str, name: str, description: str, content: dict, content_hash: str
    ) -> tuple[DatasetRow, bool]:
        """Append a version unless nothing changed. Returns (row, created)."""
        with self._sf.begin() as s:
            latest = s.scalars(
                select(DatasetRow).where(DatasetRow.id == dataset_id).order_by(DatasetRow.version.desc()).limit(1)
            ).first()
            assert latest is not None
            if latest.content_hash == content_hash and latest.name == name and latest.description == description:
                return latest, False
            row = DatasetRow(
                id=dataset_id,
                version=latest.version + 1,
                project_id=latest.project_id,
                name=name,
                description=description,
                content=content,
                content_hash=content_hash,
            )
            s.add(row)
        return row, True

    def list_datasets(self, project_id: str) -> list[DatasetRow]:
        with self._sf() as s:
            latest = (
                select(DatasetRow.id, func.max(DatasetRow.version).label("v"))
                .where(DatasetRow.project_id == project_id)
                .group_by(DatasetRow.id)
                .subquery()
            )
            stmt = (
                select(DatasetRow)
                .join(latest, (DatasetRow.id == latest.c.id) & (DatasetRow.version == latest.c.v))
                .order_by(DatasetRow.created_at.desc())
            )
            return list(s.scalars(stmt))

    def dataset_versions(self, dataset_id: str) -> list[DatasetRow]:
        with self._sf() as s:
            return list(s.scalars(select(DatasetRow).where(DatasetRow.id == dataset_id).order_by(DatasetRow.version)))

    def delete_dataset(self, dataset_id: str) -> int:
        with self._sf.begin() as s:
            return s.execute(delete(DatasetRow).where(DatasetRow.id == dataset_id)).rowcount

    # -- evaluation runs --------------------------------------------------------------------------
    def create_eval_run(self, **fields: Any) -> EvalRunRow:
        row = EvalRunRow(id=f"ev_{uuid.uuid4().hex[:12]}", status="queued", **fields)
        with self._sf.begin() as s:
            s.add(row)
        return row

    def update_eval_run(self, eval_id: str, **fields: Any) -> None:
        if fields.get("status") in TERMINAL:
            fields.setdefault("finished_at", utcnow())
        with self._sf.begin() as s:
            s.execute(update(EvalRunRow).where(EvalRunRow.id == eval_id).values(**fields))

    def increment_done(self, eval_id: str) -> None:
        with self._sf.begin() as s:
            s.execute(update(EvalRunRow).where(EvalRunRow.id == eval_id).values(done=EvalRunRow.done + 1))

    def get_eval_run(self, eval_id: str) -> EvalRunRow | None:
        with self._sf() as s:
            return s.get(EvalRunRow, eval_id)

    def list_eval_runs(self, project_id: str) -> list[EvalRunRow]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(EvalRunRow).where(EvalRunRow.project_id == project_id).order_by(EvalRunRow.created_at.desc())
                )
            )

    def delete_eval_run(self, eval_id: str) -> int:
        with self._sf.begin() as s:
            return s.execute(delete(EvalRunRow).where(EvalRunRow.id == eval_id)).rowcount

    def set_baseline(self, eval_id: str) -> None:
        with self._sf.begin() as s:
            row = s.get(EvalRunRow, eval_id)
            assert row is not None
            s.execute(
                update(EvalRunRow)
                .where(EvalRunRow.project_id == row.project_id, EvalRunRow.dataset_id == row.dataset_id)
                .values(baseline=False)
            )
            row.baseline = True

    def fail_interrupted(self) -> int:
        with self._sf.begin() as s:
            rows = s.scalars(select(EvalRunRow).where(EvalRunRow.status.in_(["queued", "running"]))).all()
            for row in rows:
                row.status = "failed"
                row.error = {"code": "EVAL_INTERRUPTED", "message": "The server restarted during this evaluation"}
                row.finished_at = utcnow()
            return len(rows)

    # -- case results -----------------------------------------------------------------------------
    def put_case(self, eval_id: str, position: int, case_id: str, run_id: str, status: str, payload: dict) -> None:
        with self._sf.begin() as s:
            s.merge(
                EvalCaseRow(
                    eval_run_id=eval_id,
                    case_id=case_id,
                    position=position,
                    run_id=run_id,
                    status=status,
                    payload=payload,
                )
            )

    def list_cases(self, eval_id: str) -> list[EvalCaseRow]:
        with self._sf() as s:
            return list(
                s.scalars(select(EvalCaseRow).where(EvalCaseRow.eval_run_id == eval_id).order_by(EvalCaseRow.position))
            )

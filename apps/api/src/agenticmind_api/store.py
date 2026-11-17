"""Repository layer over the persistence models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from agenticmind_ir import ProjectDocument, content_hash, to_canonical_dict
from agenticmind_runtime import RunResult, RunStatus, TraceEvent
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from .db import NodeOutputRow, ProjectRevisionRow, ProjectRow, RunEventRow, RunRow, utcnow


class ConflictError(Exception):
    pass


class Store:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory

    # -- projects --------------------------------------------------------------------------
    def create_project(self, doc: ProjectDocument) -> tuple[ProjectRow, ProjectRevisionRow]:
        with self._sf.begin() as s:
            row = ProjectRow(
                id=doc.project.id,
                name=doc.project.name,
                description=doc.project.description,
                current_revision=1,
            )
            rev = ProjectRevisionRow(
                project_id=doc.project.id,
                revision=1,
                document=to_canonical_dict(doc),
                content_hash=content_hash(doc),
            )
            s.add(row)
            s.flush()
            s.add(rev)
        return row, rev

    def list_projects(self) -> list[ProjectRow]:
        with self._sf() as s:
            return list(s.scalars(select(ProjectRow).order_by(ProjectRow.updated_at.desc())))

    def get_project(self, project_id: str) -> ProjectRow | None:
        with self._sf() as s:
            return s.get(ProjectRow, project_id)

    def get_revision(self, project_id: str, revision: int | None = None) -> ProjectRevisionRow | None:
        with self._sf() as s:
            if revision is None:
                proj = s.get(ProjectRow, project_id)
                if proj is None:
                    return None
                revision = proj.current_revision
            return s.get(ProjectRevisionRow, (project_id, revision))

    def list_revisions(self, project_id: str) -> list[ProjectRevisionRow]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(ProjectRevisionRow)
                    .where(ProjectRevisionRow.project_id == project_id)
                    .order_by(ProjectRevisionRow.revision.desc())
                )
            )

    def save_revision(
        self, project_id: str, doc: ProjectDocument, expected_revision: int | None
    ) -> tuple[ProjectRow, ProjectRevisionRow, bool]:
        """Append a revision. Returns ``created=False`` when content is unchanged (no-op save)."""
        with self._sf.begin() as s:
            proj = s.get(ProjectRow, project_id, with_for_update=True)
            if proj is None:
                raise KeyError(project_id)
            if expected_revision is not None and expected_revision != proj.current_revision:
                raise ConflictError(
                    f"Project is at revision {proj.current_revision}, expected {expected_revision}"
                )
            current = s.get(ProjectRevisionRow, (project_id, proj.current_revision))
            new_hash = content_hash(doc)
            if current is not None and current.content_hash == new_hash:
                return proj, current, False
            proj.current_revision += 1
            proj.name = doc.project.name
            proj.description = doc.project.description
            proj.updated_at = utcnow()
            rev = ProjectRevisionRow(
                project_id=project_id,
                revision=proj.current_revision,
                document=to_canonical_dict(doc),
                content_hash=new_hash,
            )
            s.add(rev)
        return proj, rev, True

    def delete_project(self, project_id: str) -> bool:
        with self._sf.begin() as s:
            proj = s.get(ProjectRow, project_id)
            if proj is None:
                return False
            run_ids = select(RunRow.id).where(RunRow.project_id == project_id)
            s.execute(delete(RunEventRow).where(RunEventRow.run_id.in_(run_ids)))
            s.execute(delete(NodeOutputRow).where(NodeOutputRow.run_id.in_(run_ids)))
            s.execute(delete(RunRow).where(RunRow.project_id == project_id))
            s.execute(delete(ProjectRevisionRow).where(ProjectRevisionRow.project_id == project_id))
            s.delete(proj)
        return True

    # -- runs ------------------------------------------------------------------------------
    def create_run(
        self, run_id: str, project_id: str, revision: ProjectRevisionRow, inputs: Mapping[str, Any]
    ) -> RunRow:
        with self._sf.begin() as s:
            row = RunRow(
                id=run_id,
                project_id=project_id,
                revision=revision.revision,
                status=RunStatus.QUEUED.value,
                inputs=dict(inputs),
                ir_snapshot=revision.document,
                content_hash=revision.content_hash,
            )
            s.add(row)
        return row

    def mark_run_started(self, run_id: str) -> None:
        with self._sf.begin() as s:
            row = s.get(RunRow, run_id)
            if row:
                row.status = RunStatus.RUNNING.value
                row.started_at = utcnow()

    def finish_run(self, result: RunResult) -> None:
        with self._sf.begin() as s:
            row = s.get(RunRow, result.run_id)
            if row:
                row.status = result.status.value
                row.outputs = result.outputs
                row.error = result.error.model_dump() if result.error else None
                row.node_status = {k: v.value for k, v in result.node_status.items()}
                row.finished_at = result.finished_at

    def fail_run(self, run_id: str, code: str, message: str) -> None:
        with self._sf.begin() as s:
            row = s.get(RunRow, run_id)
            if row and not RunStatus(row.status).terminal:
                row.status = RunStatus.FAILED.value
                row.error = {"code": code, "message": message, "node_id": None, "retryable": False}
                row.finished_at = utcnow()

    def fail_orphaned_runs(self) -> int:
        """Runs left non-terminal by a previous process cannot resume yet (no durable checkpoints)."""
        with self._sf.begin() as s:
            rows = s.scalars(
                select(RunRow).where(RunRow.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]))
            ).all()
            for row in rows:
                row.status = RunStatus.FAILED.value
                row.error = {
                    "code": "RUN_INTERRUPTED",
                    "message": "The server restarted while this run was active; durable resume is not yet supported",
                    "node_id": None,
                    "retryable": False,
                }
                row.finished_at = utcnow()
            return len(rows)

    def get_run(self, run_id: str) -> RunRow | None:
        with self._sf() as s:
            return s.get(RunRow, run_id)

    def list_runs(self, project_id: str, limit: int = 50) -> list[RunRow]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(RunRow)
                    .where(RunRow.project_id == project_id)
                    .order_by(RunRow.created_at.desc())
                    .limit(limit)
                )
            )

    # -- events & outputs ------------------------------------------------------------------
    def add_event(self, event: TraceEvent) -> None:
        with self._sf.begin() as s:
            s.add(
                RunEventRow(
                    run_id=event.run_id,
                    seq=event.seq,
                    event_type=event.event_type.value,
                    node_id=event.node_id,
                    timestamp=event.timestamp,
                    payload=event.model_dump(mode="json", exclude_none=True),
                )
            )

    def list_events(self, run_id: str, after: int = 0, limit: int = 1000) -> list[RunEventRow]:
        with self._sf() as s:
            return list(
                s.scalars(
                    select(RunEventRow)
                    .where(RunEventRow.run_id == run_id, RunEventRow.seq > after)
                    .order_by(RunEventRow.seq)
                    .limit(limit)
                )
            )

    def event_count(self, run_id: str) -> int:
        with self._sf() as s:
            return s.scalar(select(func.count()).where(RunEventRow.run_id == run_id)) or 0

    def put_node_outputs(self, run_id: str, node_id: str, invocation_id: str, outputs: Mapping[str, Any]) -> None:
        with self._sf.begin() as s:
            row = s.get(NodeOutputRow, (run_id, node_id))
            if row is None:
                s.add(NodeOutputRow(run_id=run_id, node_id=node_id, invocation_id=invocation_id, outputs=dict(outputs)))
            else:
                row.invocation_id = invocation_id
                row.outputs = dict(outputs)

    def get_node_outputs(self, run_id: str, node_id: str) -> NodeOutputRow | None:
        with self._sf() as s:
            return s.get(NodeOutputRow, (run_id, node_id))


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None

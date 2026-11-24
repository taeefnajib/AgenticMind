"""Repository layer over the persistence models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from agenticmind_ir import ProjectDocument, content_hash, to_canonical_dict
from agenticmind_runtime import NodeCheckpoint, RunResult, RunStatus, TraceEvent
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .db import (
    LeaseRow,
    NodeOutputRow,
    ProjectRevisionRow,
    ProjectRow,
    RunCheckpointRow,
    RunEventRow,
    RunJobRow,
    RunRow,
    RunWaitRow,
    utcnow,
)


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
                raise ConflictError(f"Project is at revision {proj.current_revision}, expected {expected_revision}")
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
        self,
        run_id: str,
        project_id: str,
        revision: ProjectRevisionRow,
        inputs: Mapping[str, Any],
        *,
        targets: Sequence[str] | None = None,
        enqueue: bool = True,
    ) -> RunRow:
        """Create a queued run and (atomically) its execution job."""
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
            if enqueue:
                s.flush()
                s.add(RunJobRow(run_id=run_id, kind="start", status="queued", targets=list(targets or [])))
        return row

    # -- durable execution queue (ADR-0039) --------------------------------------------------
    def enqueue_resume(
        self, run_id: str, *, responses: Mapping[str, Any], targets: Sequence[str] | None, force: bool
    ) -> None:
        with self._sf.begin() as s:
            job = s.get(RunJobRow, run_id)
            if job is None:
                job = RunJobRow(run_id=run_id)
                s.add(job)
            job.kind, job.status, job.owner, job.lease_expires_at = "resume", "queued", None, None
            job.attempts, job.responses, job.force, job.cancel_requested = 0, dict(responses), force, False
            job.targets = list(targets or [])
            job.updated_at = utcnow()

    def get_job(self, run_id: str) -> RunJobRow | None:
        with self._sf() as s:
            return s.get(RunJobRow, run_id)

    def claim_jobs(self, owner: str, lease_seconds: float, limit: int) -> list[RunJobRow]:
        """Claim up to ``limit`` queued jobs, or leased jobs whose worker stopped renewing (expired lease).

        Each claim is one conditional UPDATE, so two workers can never hold the same job.
        """
        if limit <= 0:
            return []
        now = utcnow()
        claimable = (RunJobRow.status == "queued") | (
            (RunJobRow.status == "leased") & (RunJobRow.lease_expires_at < now)
        )
        with self._sf() as s:
            candidates = list(
                s.scalars(select(RunJobRow.run_id).where(claimable).order_by(RunJobRow.created_at).limit(limit * 3))
            )
        claimed: list[RunJobRow] = []
        for run_id in candidates:
            if len(claimed) >= limit:
                break
            with self._sf.begin() as s:
                result = s.execute(
                    update(RunJobRow)
                    .where(RunJobRow.run_id == run_id, claimable)
                    .values(
                        status="leased",
                        owner=owner,
                        attempts=RunJobRow.attempts + 1,
                        updated_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                    )
                )
                if result.rowcount == 1:
                    claimed.append(s.get(RunJobRow, run_id))
        return claimed

    def renew_lease(self, run_id: str, owner: str, lease_seconds: float) -> tuple[bool, bool]:
        """(still the owner, cancel requested)."""
        now = utcnow()
        with self._sf.begin() as s:
            result = s.execute(
                update(RunJobRow)
                .where(RunJobRow.run_id == run_id, RunJobRow.owner == owner, RunJobRow.status == "leased")
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
            )
            job = s.get(RunJobRow, run_id)
            return result.rowcount == 1, bool(job and job.cancel_requested)

    def finish_job(self, run_id: str, owner: str) -> None:
        with self._sf.begin() as s:
            s.execute(
                update(RunJobRow)
                .where(RunJobRow.run_id == run_id, RunJobRow.owner == owner)
                .values(status="done", lease_expires_at=None, updated_at=utcnow())
            )

    def release_job(self, run_id: str, owner: str) -> None:
        """Hand a job back (graceful shutdown): another worker picks it up and resumes from checkpoints."""
        with self._sf.begin() as s:
            s.execute(
                update(RunJobRow)
                .where(RunJobRow.run_id == run_id, RunJobRow.owner == owner, RunJobRow.status == "leased")
                .values(status="queued", owner=None, lease_expires_at=None, updated_at=utcnow())
            )

    def request_cancel(self, run_id: str) -> str:
        """Cancel a queued run now ("cancelled"), flag a running one for its worker ("requested"), else ""."""
        with self._sf.begin() as s:
            job = s.get(RunJobRow, run_id, with_for_update=True)
            run = s.get(RunRow, run_id)
            if job is None or job.status == "done" or run is None:
                return ""
            if job.status == "queued":
                job.status = "done"
                if not RunStatus(run.status).terminal:
                    run.status = RunStatus.CANCELLED.value
                    run.finished_at = utcnow()
                return "cancelled"
            job.cancel_requested = True
            return "requested"

    def acquire_lease(self, name: str, owner: str, lease_seconds: float) -> bool:
        """Take or renew a named lease; True while this owner holds it."""
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        with self._sf.begin() as s:
            result = s.execute(
                update(LeaseRow)
                .where(LeaseRow.name == name, (LeaseRow.owner == owner) | (LeaseRow.expires_at < now))
                .values(owner=owner, expires_at=expires)
            )
            if result.rowcount == 1:
                return True
            if s.get(LeaseRow, name) is not None:
                return False
            try:
                with s.begin_nested():
                    s.add(LeaseRow(name=name, owner=owner, expires_at=expires))
            except IntegrityError:
                return False
            return True

    def release_lease(self, name: str, owner: str) -> None:
        with self._sf.begin() as s:
            s.execute(
                update(LeaseRow).where(LeaseRow.name == name, LeaseRow.owner == owner).values(expires_at=utcnow())
            )

    def fail_unqueued_orphans(self) -> int:
        """Runs left active without a job (created before the queue existed): interrupted, resumable."""
        with self._sf.begin() as s:
            jobs = select(RunJobRow.run_id)
            rows = s.scalars(
                select(RunRow).where(
                    RunRow.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]), RunRow.id.not_in(jobs)
                )
            ).all()
            for row in rows:
                row.status = RunStatus.FAILED.value
                row.error = {
                    "code": "RUN_INTERRUPTED",
                    "message": "The server restarted while this run was active. "
                    "Resume it to continue from its last checkpoint.",
                    "node_id": None,
                    "retryable": False,
                }
                row.finished_at = utcnow()
            return len(rows)

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
                row.finished_at = None if result.status == RunStatus.WAITING else result.finished_at
                for w in result.waiting:
                    pending = s.scalar(
                        select(RunWaitRow).where(
                            RunWaitRow.run_id == row.id, RunWaitRow.node_id == w.node_id, RunWaitRow.status == "pending"
                        )
                    )
                    if pending is None:
                        s.add(
                            RunWaitRow(
                                run_id=row.id,
                                project_id=row.project_id,
                                node_id=w.node_id,
                                node_type=w.node_type,
                                request=w.request,
                                response_schema=w.response_schema,
                            )
                        )

    def fail_run(self, run_id: str, code: str, message: str) -> None:
        with self._sf.begin() as s:
            row = s.get(RunRow, run_id)
            if row and not RunStatus(row.status).terminal:
                row.status = RunStatus.FAILED.value
                row.error = {"code": code, "message": message, "node_id": None, "retryable": False}
                row.finished_at = utcnow()

    def fail_orphaned_runs(self) -> int:
        """Mark runs a previous process left active as interrupted; they can be resumed from checkpoints.

        Waiting runs are durable and untouched.
        """
        with self._sf.begin() as s:
            rows = s.scalars(
                select(RunRow).where(RunRow.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]))
            ).all()
            for row in rows:
                row.status = RunStatus.FAILED.value
                row.error = {
                    "code": "RUN_INTERRUPTED",
                    "message": "The server restarted while this run was active. Resume it to continue "
                    "from its last checkpoint.",
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

    # -- checkpoints & waits (ADR-0013) ---------------------------------------------------
    def put_checkpoint(self, run_id: str, checkpoint: NodeCheckpoint) -> None:
        payload = checkpoint.model_dump(mode="json")
        with self._sf.begin() as s:
            row = s.get(RunCheckpointRow, (run_id, checkpoint.node_id))
            if row is None:
                s.add(RunCheckpointRow(run_id=run_id, node_id=checkpoint.node_id, payload=payload))
            else:
                row.payload = payload
                row.created_at = utcnow()

    def list_checkpoints(self, run_id: str) -> list[NodeCheckpoint]:
        with self._sf() as s:
            rows = s.scalars(select(RunCheckpointRow).where(RunCheckpointRow.run_id == run_id))
            return [NodeCheckpoint.model_validate(r.payload) for r in rows]

    def list_waits(
        self, *, run_id: str | None = None, project_id: str | None = None, status: str | None = None
    ) -> list[RunWaitRow]:
        with self._sf() as s:
            stmt = select(RunWaitRow)
            if run_id:
                stmt = stmt.where(RunWaitRow.run_id == run_id)
            if project_id:
                stmt = stmt.where(RunWaitRow.project_id == project_id)
            if status:
                stmt = stmt.where(RunWaitRow.status == status)
            return list(s.scalars(stmt.order_by(RunWaitRow.created_at.desc()).limit(500)))

    def claim_for_resume(self, run_id: str, responses: Mapping[str, Any]) -> bool:
        """Atomically move a waiting/interrupted run back to running and record the responses."""
        with self._sf.begin() as s:
            row = s.get(RunRow, run_id, with_for_update=True)
            interrupted = (
                row is not None
                and row.status == RunStatus.FAILED.value
                and (row.error or {}).get("code") == "RUN_INTERRUPTED"
            )
            if row is None or not (row.status == RunStatus.WAITING.value or interrupted):
                return False
            row.status = RunStatus.RUNNING.value
            row.error = None
            row.finished_at = None
            now = utcnow()
            for w in s.scalars(select(RunWaitRow).where(RunWaitRow.run_id == run_id, RunWaitRow.status == "pending")):
                if w.node_id in responses:
                    w.status = "answered"
                    w.response = responses[w.node_id]
                    w.answered_at = now
            return True

    def cancel_waiting_run(self, run_id: str) -> bool:
        with self._sf.begin() as s:
            row = s.get(RunRow, run_id, with_for_update=True)
            if row is None or row.status != RunStatus.WAITING.value:
                return False
            row.status = RunStatus.CANCELLED.value
            row.finished_at = utcnow()
            for w in s.scalars(select(RunWaitRow).where(RunWaitRow.run_id == run_id, RunWaitRow.status == "pending")):
                w.status = "cancelled"
            return True

    def last_seq(self, run_id: str) -> int:
        with self._sf() as s:
            return s.scalar(select(func.max(RunEventRow.seq)).where(RunEventRow.run_id == run_id)) or 0

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

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from agenticmind_ir import load_project
from agenticmind_runtime import PlanError, RunStatus
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..db import RunRow
from ..deps import Services, services
from ..schemas import RunCreate, RunOut
from ..store import iso

router = APIRouter(prefix="/api", tags=["runs"])

SSE_POLL_SECONDS = 0.2
SSE_KEEPALIVE_SECONDS = 15.0


def _run_out(row: RunRow) -> RunOut:
    return RunOut(
        id=row.id,
        project_id=row.project_id,
        revision=row.revision,
        status=row.status,
        inputs=row.inputs,
        outputs=row.outputs,
        error=row.error,
        node_status=row.node_status,
        content_hash=row.content_hash,
        created_at=iso(row.created_at),
        started_at=iso(row.started_at),
        finished_at=iso(row.finished_at),
    )


def _require_run(svc: Services, run_id: str) -> RunRow:
    row = svc.store.get_run(run_id)
    if row is None:
        raise HTTPException(404, f"Run {run_id!r} not found")
    return row


@router.post("/projects/{project_id}/runs", response_model=RunOut, status_code=202)
async def start_run(project_id: str, body: RunCreate, svc: Services = Depends(services)) -> RunOut:
    """Start a run of a saved revision. Returns 422 with diagnostics if the graph is not executable."""
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")
    rev = svc.store.get_revision(project_id, body.revision)
    if rev is None:
        raise HTTPException(404, "Revision not found")
    try:
        plan = svc.runs.plan(load_project(rev.document))
    except PlanError as exc:
        raise HTTPException(422, {"message": str(exc), **exc.report.to_public()}) from exc
    run_id = svc.runs.start(project_id, rev, plan, body.inputs)
    return _run_out(_require_run(svc, run_id))


@router.get("/projects/{project_id}/runs", response_model=list[RunOut])
def list_runs(project_id: str, limit: int = 50, svc: Services = Depends(services)) -> list[RunOut]:
    return [_run_out(r) for r in svc.store.list_runs(project_id, limit=min(limit, 200))]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str, svc: Services = Depends(services)) -> RunOut:
    return _run_out(_require_run(svc, run_id))


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(run_id: str, svc: Services = Depends(services)) -> RunOut:
    row = _require_run(svc, run_id)
    if RunStatus(row.status).terminal:
        raise HTTPException(409, f"Run is already {row.status}")
    if not svc.runs.cancel(run_id):
        raise HTTPException(409, "Run is not active in this process")
    return _run_out(row)


@router.get("/runs/{run_id}/nodes/{node_id}/outputs")
def node_outputs(run_id: str, node_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    _require_run(svc, run_id)
    row = svc.store.get_node_outputs(run_id, node_id)
    if row is None:
        raise HTTPException(404, "No outputs recorded for this node in this run")
    return {"run_id": run_id, "node_id": node_id, "invocation_id": row.invocation_id, "outputs": row.outputs}


@router.get("/runs/{run_id}/events", response_model=None)
async def run_events(
    run_id: str,
    request: Request,
    after: int = 0,
    accept: str | None = Header(default=None),
    last_event_id: str | None = Header(default=None),
    svc: Services = Depends(services),
):
    """Trace events in ``seq`` order.

    * JSON (default): events with ``seq > after``.
    * SSE (``Accept: text/event-stream``): live stream; each message ``id`` is the event ``seq``,
      so browsers resume via ``Last-Event-ID`` after reconnecting. Ends with an ``end`` event once
      the run is terminal and all events were delivered. Events are retained with the run.
    """
    _require_run(svc, run_id)
    if not accept or "text/event-stream" not in accept:
        return [e.payload for e in svc.store.list_events(run_id, after=after)]

    start_after = after
    if last_event_id and last_event_id.isdigit():
        start_after = max(start_after, int(last_event_id))

    async def stream() -> AsyncIterator[str]:
        last = start_after
        idle = 0.0
        yield "retry: 1000\n\n"
        while True:
            if await request.is_disconnected():
                return
            run = svc.store.get_run(run_id)
            terminal = run is not None and RunStatus(run.status).terminal
            events = svc.store.list_events(run_id, after=last)
            for e in events:
                last = e.seq
                yield f"id: {e.seq}\nevent: trace\ndata: {json.dumps(e.payload)}\n\n"
            if terminal and not events:
                yield f"event: end\ndata: {json.dumps({'status': run.status if run else None})}\n\n"
                return
            if events:
                idle = 0.0
            else:
                idle += SSE_POLL_SECONDS
                if idle >= SSE_KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": keepalive\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

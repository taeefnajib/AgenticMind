from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from agenticmind_ir import load_project
from agenticmind_runtime import EventType, PlanError, ResumeState, RunStatus, TraceEvent
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..db import RunRow, RunWaitRow
from ..deps import Services, services
from ..schemas import RunCreate, RunOut, RunResume, WaitOut
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
        plan = svc.runs.plan(load_project(rev.document), targets=body.targets)
    except PlanError as exc:
        raise HTTPException(422, {"message": str(exc), **exc.report.to_public()}) from exc
    run_id = svc.runs.start(project_id, rev, plan, body.inputs)
    return _run_out(_require_run(svc, run_id))


@router.post("/projects/{project_id}/runs/stream", response_model=None)
async def start_run_streaming(
    project_id: str, body: RunCreate, request: Request, svc: Services = Depends(services)
) -> StreamingResponse:
    """Start a run and stream it in one call (ADR-0040): ``event: run`` (id), ``token`` deltas as models write,
    ``trace`` events, then ``result`` (status, outputs, error) and ``end``. Add ``?trace=false`` to skip traces."""
    created = await start_run(project_id, body, svc)
    trace = request.query_params.get("trace", "true").lower() != "false"

    async def body_stream() -> AsyncIterator[str]:
        yield f"event: run\ndata: {json.dumps({'run_id': created.id})}\n\n"
        async for message in sse_stream(svc, created.id, request, 0, tokens=True, result=True):
            if not trace and message.startswith("id: "):
                continue
            yield message

    return StreamingResponse(
        body_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


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
    if row.status == RunStatus.WAITING.value:
        if not svc.store.cancel_waiting_run(run_id):
            raise HTTPException(409, "Run is no longer waiting")
        svc.store.add_event(
            TraceEvent(
                run_id=run_id,
                project_id=row.project_id,
                seq=svc.store.last_seq(run_id) + 1,
                event_type=EventType.RUN_CANCELLED,
                status=RunStatus.CANCELLED.value,
                data={"reason": "cancelled_while_waiting"},
            )
        )
        return _run_out(_require_run(svc, run_id))
    if not svc.runs.cancel(run_id):
        raise HTTPException(409, "Run is not active in this process")
    return _run_out(row)


def _wait_out(w: RunWaitRow) -> WaitOut:
    return WaitOut(
        id=w.id,
        run_id=w.run_id,
        project_id=w.project_id,
        node_id=w.node_id,
        node_type=w.node_type,
        request=w.request,
        response_schema=w.response_schema,
        status=w.status,
        response=w.response,
        created_at=iso(w.created_at),
        answered_at=iso(w.answered_at),
    )


@router.get("/runs/{run_id}/waits", response_model=list[WaitOut])
def run_waits(run_id: str, svc: Services = Depends(services)) -> list[WaitOut]:
    _require_run(svc, run_id)
    return [_wait_out(w) for w in svc.store.list_waits(run_id=run_id)]


@router.get("/projects/{project_id}/approvals", response_model=list[WaitOut])
def project_approvals(
    project_id: str, status: str | None = "pending", svc: Services = Depends(services)
) -> list[WaitOut]:
    """The project's approval inbox (pending by default; ``status=answered|cancelled``, or empty for all)."""
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")
    return [_wait_out(w) for w in svc.store.list_waits(project_id=project_id, status=status or None)]


def _run_targets(svc: Services, run_id: str) -> list[str]:
    first = svc.store.list_events(run_id, after=0, limit=1)
    data = first[0].payload.get("data", {}) if first else {}
    return list(data.get("targets") or [])


@router.post("/runs/{run_id}/resume", response_model=RunOut, status_code=202)
async def resume_run(run_id: str, body: RunResume, svc: Services = Depends(services)) -> RunOut:
    """Continue a waiting run with responses, or an interrupted run from its last checkpoints (ADR-0013)."""
    from jsonschema import Draft202012Validator

    row = _require_run(svc, run_id)
    interrupted = row.status == RunStatus.FAILED.value and (row.error or {}).get("code") == "RUN_INTERRUPTED"
    if row.status != RunStatus.WAITING.value and not interrupted:
        raise HTTPException(409, f"Only waiting or interrupted runs can be resumed (this run is {row.status})")
    if svc.runs.is_active(run_id):
        raise HTTPException(409, "Run is already executing")
    pending = {w.node_id: w for w in svc.store.list_waits(run_id=run_id, status="pending")}
    unknown = sorted(set(body.responses) - set(pending))
    if unknown:
        raise HTTPException(422, {"message": "These nodes are not waiting for a response", "nodes": unknown})
    problems: dict[str, list[str]] = {}
    for node_id, response in body.responses.items():
        schema = pending[node_id].response_schema
        if schema:
            errs = sorted(Draft202012Validator(schema).iter_errors(response), key=lambda e: list(e.path))
            if errs:
                problems[node_id] = [f"{'/'.join(map(str, e.path)) or 'response'}: {e.message}" for e in errs[:5]]
    if problems:
        raise HTTPException(422, {"message": "Invalid response", "problems": problems})
    try:
        plan = svc.runs.plan(load_project(row.ir_snapshot), targets=_run_targets(svc, run_id) or None)
    except PlanError as exc:
        raise HTTPException(422, {"message": str(exc), **exc.report.to_public()}) from exc
    if interrupted and not body.force:
        unsafe = svc.runs.unfinished_side_effects(run_id, plan)
        if unsafe:
            raise HTTPException(
                409,
                {
                    "message": "These nodes were running when the server stopped and may have side effects; "
                    "resume with force=true to run them again",
                    "nodes": unsafe,
                },
            )
    if not svc.store.claim_for_resume(run_id, body.responses):
        raise HTTPException(409, "Run was resumed or cancelled by someone else")
    state = ResumeState(
        checkpoints=svc.store.list_checkpoints(run_id),
        responses=body.responses,
        last_seq=svc.store.last_seq(run_id),
    )
    svc.runs.resume(run_id, plan, row.inputs, state, force=body.force)
    return _run_out(_require_run(svc, run_id))


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
    tokens: bool = True,
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

    return StreamingResponse(
        sse_stream(svc, run_id, request, start_after, tokens=tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def sse_stream(
    svc: Services, run_id: str, request: Request, start_after: int = 0, *, tokens: bool = True, result: bool = False
) -> AsyncIterator[str]:
    """Trace events (``event: trace``, ``id`` = seq), streamed text (``event: token``, with its chunk ``seq``)
    and, with ``result``, a final ``event: result`` carrying status, outputs and error (ADR-0040)."""
    last = start_after
    last_chunk = 0
    idle = 0.0
    yield "retry: 1000\n\n"
    while True:
        if await request.is_disconnected():
            return
        run = svc.store.get_run(run_id)
        # a waiting run has no more events until it is resumed (clients reconnect after resuming)
        terminal = run is not None and (
            RunStatus(run.status).terminal or (run.status == RunStatus.WAITING.value and not svc.runs.is_active(run_id))
        )
        chunks = svc.store.list_stream_chunks(run_id, after=last_chunk) if tokens else []
        for c in chunks:
            last_chunk = c.seq
            data = {"seq": c.seq, "node_id": c.node_id, "invocation_id": c.invocation_id, "text": c.text}
            yield f"event: token\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        events = svc.store.list_events(run_id, after=last)
        for e in events:
            last = e.seq
            yield f"id: {e.seq}\nevent: trace\ndata: {json.dumps(e.payload)}\n\n"
        if terminal and not events and not chunks:
            if result and run is not None:
                final = {"run_id": run_id, "status": run.status, "outputs": run.outputs, "error": run.error}
                yield f"event: result\ndata: {json.dumps(final, ensure_ascii=False, default=str)}\n\n"
            yield f"event: end\ndata: {json.dumps({'status': run.status if run else None})}\n\n"
            return
        if events or chunks:
            idle = 0.0
        else:
            idle += SSE_POLL_SECONDS
            if idle >= SSE_KEEPALIVE_SECONDS:
                idle = 0.0
                yield ": keepalive\n\n"
        await asyncio.sleep(SSE_POLL_SECONDS)

"""Observability endpoints (ADR-0037): Prometheus metrics, OTLP trace download / export, exporter settings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agenticmind_observability import ExportError, Span, export_spans, to_otlp_json
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse

from ..deps import Services, services
from ..observability import OBSERVABILITY_KEY, ObservabilitySettingsError, validate_settings
from ..security import current_user, require_admin

router = APIRouter(prefix="/api", tags=["observability"])


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(svc: Services = Depends(services)) -> PlainTextResponse:
    active = sum(1 for _ in getattr(svc.runs, "_active", {}))
    text = svc.observability.metrics.render({"agenticmind_active_runs": active})
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")


def _require_run(svc: Services, run_id: str) -> None:
    if svc.store.get_run(run_id) is None:
        raise HTTPException(404, f"Run {run_id!r} not found")


@router.get("/runs/{run_id}/otlp")
def download_trace(
    run_id: str, include_content: bool = Query(default=False), svc: Services = Depends(services)
) -> JSONResponse:
    """The run as an OTLP/JSON ExportTraceServiceRequest (import into any OpenTelemetry tool)."""
    _require_run(svc, run_id)
    spans = svc.observability.spans(run_id, include_content=include_content)
    config, _ = svc.observability.config()
    body = to_otlp_json(spans, config.service_name if config else "agenticmind")
    return JSONResponse(body, headers={"Content-Disposition": f'attachment; filename="{run_id}.otlp.json"'})


@router.post("/runs/{run_id}/export")
async def export_now(run_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    _require_run(svc, run_id)
    try:
        count = await svc.observability.export_run(run_id)
    except ExportError as exc:
        raise HTTPException(502, {"code": "EXPORT_FAILED", "message": str(exc)}) from exc
    return {"ok": True, "spans": count}


def _state(svc: Services, request_user: Any) -> dict[str, Any]:
    config, source = svc.observability.config()
    row = svc.auth.get_setting(OBSERVABILITY_KEY)
    return {
        "source": source,
        "settings": svc.observability.workspace_settings(),
        "effective": config.public() if config else None,
        "status": svc.observability.status,
        "can_edit": svc.settings.auth_mode == "off" or (request_user is not None and request_user.role == "admin"),
        "updated_at": row.updated_at.isoformat() if row else None,
    }


@router.get("/settings/observability")
def get_settings(svc: Services = Depends(services), user: Any = Depends(current_user)) -> dict[str, Any]:
    return _state(svc, user)


@router.put("/settings/observability", dependencies=[Depends(require_admin)])
def put_settings(
    body: dict[str, Any] = Body(...), svc: Services = Depends(services), user: Any = Depends(current_user)
) -> dict[str, Any]:
    try:
        value = validate_settings(body)
    except ObservabilitySettingsError as exc:
        raise HTTPException(422, {"code": "SETTINGS_INVALID", "field": exc.field, "message": str(exc)}) from exc
    svc.auth.set_setting(OBSERVABILITY_KEY, value, user_id=getattr(user, "id", None))
    return _state(svc, user)


@router.delete("/settings/observability", dependencies=[Depends(require_admin)])
def reset_settings(svc: Services = Depends(services), user: Any = Depends(current_user)) -> dict[str, Any]:
    svc.auth.set_setting(OBSERVABILITY_KEY, None, user_id=getattr(user, "id", None))
    return _state(svc, user)


@router.post("/settings/observability/test", dependencies=[Depends(require_admin)])
async def test_export(svc: Services = Depends(services)) -> dict[str, Any]:
    """Send one small test span to the configured exporter."""
    config, source = svc.observability.config()
    if config is None:
        raise HTTPException(400, {"code": "EXPORT_NOT_CONFIGURED", "message": "No OTLP exporter is configured."})
    now = datetime.now(UTC)
    span = Span(
        trace_id=now.strftime("%Y%m%d%H%M%S%f").ljust(32, "0")[:32],
        span_id="a1b2c3d4e5f60718",
        parent_id=None,
        name="agenticmind connection test",
        start=now,
        end=now,
        attributes={"agenticmind.test": True},
    )
    try:
        await export_spans(config, [span], transport=svc.observability.transport, attempts=1)
    except ExportError as exc:
        raise HTTPException(502, {"code": "EXPORT_FAILED", "message": str(exc)}) from exc
    return {"ok": True, "url": config.url, "source": source}

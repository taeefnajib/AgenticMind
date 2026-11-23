"""Observability service (ADR-0037): metrics and OTLP export of every finished run.

The exporter configuration comes from the workspace settings (Settings → Observability) when set, otherwise
from the standard ``OTEL_EXPORTER_OTLP_*`` environment variables. Header values (API keys) are never stored
in settings: the workspace config names a secret holding ``key=value,key=value`` headers.
Exports run in the background after a run finishes and never slow or fail a run; their outcome is kept in
``status`` for the Settings page.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from agenticmind_ir import load_project
from agenticmind_observability import ExportError, OtlpConfig, RunMetrics, export_spans, parse_headers, run_to_spans
from agenticmind_observability.exporter import traces_url
from agenticmind_runtime import TraceEvent

if TYPE_CHECKING:
    from .deps import Services

log = logging.getLogger("agenticmind.api.observability")
OBSERVABILITY_KEY = "observability"
_SECRET = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class ObservabilitySettingsError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def validate_settings(raw: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(raw.get("endpoint") or "").strip()
    if not endpoint.startswith(("http://", "https://")) or len(endpoint) > 500:
        raise ObservabilitySettingsError("endpoint", "Enter the OTLP endpoint, e.g. http://localhost:4318")
    protocol = str(raw.get("protocol") or "http/protobuf")
    if protocol not in ("http/protobuf", "http/json"):
        raise ObservabilitySettingsError("protocol", "Choose http/protobuf or http/json.")
    secret = str(raw.get("headers_secret") or "").strip()
    if secret and not _SECRET.match(secret):
        raise ObservabilitySettingsError("headers_secret", "Enter a secret name (A-Z, 0-9, _), or leave it empty.")
    service = str(raw.get("service_name") or "agenticmind").strip()[:100] or "agenticmind"
    return {
        "enabled": bool(raw.get("enabled", True)),
        "endpoint": endpoint,
        "protocol": protocol,
        "headers_secret": secret or None,
        "service_name": service,
        "include_content": bool(raw.get("include_content", False)),
    }


class ObservabilityService:
    def __init__(self, svc: Services) -> None:
        self.svc = svc
        self.metrics = RunMetrics()
        self.status: dict[str, Any] = {
            "exported": 0,
            "failed": 0,
            "last_export_at": None,
            "last_error": None,
            "last_run_id": None,
        }
        self._tasks: set[asyncio.Task[Any]] = set()
        self.transport: Any = None  # replaceable in tests

    # ---- configuration ------------------------------------------------------------------------
    def workspace_settings(self) -> dict[str, Any] | None:
        row = self.svc.auth.get_setting(OBSERVABILITY_KEY)
        return dict(row.value) if row is not None else None

    def config(self) -> tuple[OtlpConfig | None, str]:
        """The exporter in force and where it comes from ("workspace", "environment" or "off")."""
        ws = self.workspace_settings()
        if ws is not None:
            if not ws.get("enabled"):
                return None, "off"
            headers = (
                parse_headers(self.svc.secrets.get(ws["headers_secret"]) or "") if ws.get("headers_secret") else {}
            )
            return OtlpConfig(
                url=traces_url(ws["endpoint"]),
                headers=headers,
                protocol=ws["protocol"],
                service_name=ws["service_name"],
                include_content=ws["include_content"],
            ), "workspace"
        try:
            env = OtlpConfig.from_env()
        except ExportError as exc:
            self.status["last_error"] = str(exc)
            return None, "off"
        return (env, "environment") if env is not None else (None, "off")

    # ---- runs -------------------------------------------------------------------------------------
    def run_events(self, run_id: str) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        after = 0
        while True:
            rows = self.svc.store.list_events(run_id, after=after, limit=1000)
            events += [TraceEvent.model_validate(r.payload) for r in rows]
            if len(rows) < 1000:
                return events
            after = rows[-1].seq

    def spans(self, run_id: str, *, include_content: bool) -> list[Any]:
        run = self.svc.store.get_run(run_id)
        if run is None:
            return []
        project = self.svc.store.get_project(run.project_id)
        labels: dict[str, str] = {}
        rev = self.svc.store.get_revision(run.project_id, run.revision)
        if rev is not None:
            try:
                doc = load_project(rev.document)
                labels = {n.id: n.ui.label for n in doc.nodes if n.ui and n.ui.label}
            except Exception:  # noqa: BLE001 - labels are cosmetic
                labels = {}
        return run_to_spans(
            self.run_events(run_id),
            project_name=project.name if project else "",
            revision=run.revision,
            include_content=include_content,
            labels=labels,
        )

    async def on_run_finished(self, run_id: str) -> None:
        events = await asyncio.to_thread(self.run_events, run_id)
        self.metrics.record_run(events)
        config, _source = self.config()
        if config is not None:
            task = asyncio.create_task(self.export_run(run_id, config))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def export_run(self, run_id: str, config: OtlpConfig | None = None) -> int:
        """Export one run now; returns the number of spans. Raises ``ExportError``."""
        config = config or self.config()[0]
        if config is None:
            raise ExportError("No OTLP exporter is configured")
        spans = await asyncio.to_thread(self.spans, run_id, include_content=config.include_content)
        try:
            await export_spans(config, spans, transport=self.transport)
        except ExportError as exc:
            self.status.update(failed=self.status["failed"] + 1, last_error=str(exc), last_run_id=run_id)
            log.warning("trace export for %s failed: %s", run_id, exc)
            raise
        self.status.update(
            exported=self.status["exported"] + 1,
            last_error=None,
            last_run_id=run_id,
            last_export_at=datetime.now(UTC).isoformat(),
        )
        return len(spans)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

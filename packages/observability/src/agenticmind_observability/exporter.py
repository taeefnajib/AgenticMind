"""OTLP/HTTP trace export configuration and transport (ADR-0037).

Configured like any OpenTelemetry exporter:

* ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` (used as-is) or ``OTEL_EXPORTER_OTLP_ENDPOINT`` (``/v1/traces`` is
  appended), e.g. ``http://localhost:4318`` or ``https://cloud.langfuse.com/api/public/otel``;
* ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` / ``OTEL_EXPORTER_OTLP_HEADERS``: ``key1=value1,key2=value2``
  (values may be percent-encoded), e.g. ``Authorization=Basic <base64(pk:sk)>`` for Langfuse;
* ``OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`` / ``OTEL_EXPORTER_OTLP_PROTOCOL``: ``http/protobuf`` (default) or
  ``http/json`` (gRPC is not supported);
* ``OTEL_SERVICE_NAME`` (default ``agenticmind``);
* ``AGENTICMIND_OTEL_INCLUDE_CONTENT=1`` to include input / output summaries (off by default).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import unquote

import httpx

from .otel import Span, to_otlp_json, to_otlp_protobuf

Protocol = Literal["http/protobuf", "http/json"]
RETRYABLE = {429, 502, 503, 504}


class ExportError(Exception):
    pass


def parse_headers(raw: str) -> dict[str, str]:
    """``k1=v1,k2=v2`` (W3C Baggage style, values percent-decoded)."""
    headers: dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip():
            headers[key.strip()] = unquote(value.strip())
    return headers


def traces_url(endpoint: str, *, signal_specific: bool = False) -> str:
    """A base endpoint gets ``/v1/traces`` appended (unless it already ends with it); a signal one is as-is."""
    endpoint = endpoint.strip()
    if signal_specific or endpoint.rstrip("/").endswith("/v1/traces"):
        return endpoint
    return endpoint.rstrip("/") + "/v1/traces"


@dataclass
class OtlpConfig:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    protocol: Protocol = "http/protobuf"
    service_name: str = "agenticmind"
    include_content: bool = False
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OtlpConfig | None:
        e = os.environ if env is None else env
        specific = e.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
        base = e.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if not specific and not base:
            return None
        protocol = (
            e.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL") or e.get("OTEL_EXPORTER_OTLP_PROTOCOL") or "http/protobuf"
        )
        protocol = protocol.strip().lower()
        if protocol not in ("http/protobuf", "http/json"):
            raise ExportError(f"OTLP protocol {protocol!r} is not supported (use http/protobuf or http/json)")
        return cls(
            url=traces_url(specific or base, signal_specific=bool(specific)),
            headers=parse_headers(
                e.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or e.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
            ),
            protocol=protocol,  # type: ignore[arg-type]
            service_name=e.get("OTEL_SERVICE_NAME", "").strip() or "agenticmind",
            include_content=e.get("AGENTICMIND_OTEL_INCLUDE_CONTENT", "0").lower() in ("1", "true", "yes", "on"),
        )

    def public(self) -> dict[str, Any]:
        """Safe to show: header names only, never values."""
        return {"url": self.url, "protocol": self.protocol, "service_name": self.service_name,
                "include_content": self.include_content, "header_names": sorted(self.headers)}  # fmt: skip


async def export_spans(
    config: OtlpConfig,
    spans: list[Span],
    *,
    resource: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    attempts: int = 3,
) -> None:
    """POST one ExportTraceServiceRequest; retries 429/502/503/504 and network errors with backoff."""
    if not spans:
        return
    if config.protocol == "http/json":
        import json

        body = json.dumps(to_otlp_json(spans, config.service_name, resource)).encode()
        content_type = "application/json"
    else:
        body = to_otlp_protobuf(spans, config.service_name, resource)
        content_type = "application/x-protobuf"
    headers = {**config.headers, "Content-Type": content_type}
    last = ""
    async with httpx.AsyncClient(timeout=config.timeout_seconds, transport=transport) as client:
        for attempt in range(attempts):
            try:
                resp = await client.post(config.url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code < 300:
                    return
                last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if resp.status_code not in RETRYABLE:
                    break
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5 * 2**attempt)
    raise ExportError(f"OTLP export to {config.url} failed: {last}")

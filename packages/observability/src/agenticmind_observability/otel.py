"""Run traces as OpenTelemetry spans, exported over OTLP/HTTP (ADR-0037).

A finished run's persisted trace events become one trace:

* a root ``invoke_workflow <project>`` span (run id, revision, status);
* one span per node invocation (loop iterations and retries get their own); a node that reports model usage
  is a GenAI ``chat <model>`` span with ``gen_ai.provider.name`` / ``gen_ai.request.model`` /
  ``gen_ai.usage.*``;
* one ``execute_tool <tool>`` child span per agent tool call (timed from its duration), and the other node
  events (agent turns, retries, skips, guardrail blocks, ...) as span events.

Ids are derived from the run id, so re-exporting a run produces the same trace. Prompt / output content is
left out unless ``include_content`` is set (summaries can contain personal data). Attributes follow the
OpenTelemetry GenAI conventions; Langfuse- and OpenInference-style keys are added for content so those
tools show input and output.

Encodings: OTLP/HTTP protobuf (default) or OTLP/JSON (hex ids, 64-bit integers as strings, enum integers).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agenticmind_runtime import TraceEvent

SCOPE_NAME = "agenticmind"
SCOPE_VERSION = "0.1.0"
KIND_INTERNAL, KIND_CLIENT = 1, 3
STATUS_UNSET, STATUS_OK, STATUS_ERROR = 0, 1, 2
_TERMINAL_RUN = {"run_succeeded", "run_failed", "run_cancelled", "run_waiting"}
_NODE_END = {"node_succeeded", "node_failed", "node_cancelled", "node_waiting", "node_skipped"}
#: Nodes that run an agent loop: ``invoke_agent`` spans (their tool calls become child spans).
AGENT_NODE_TYPES = {"agenticmind.agents.agent", "agenticmind.agents.reasoning", "agenticmind.agents.tool_router"}
#: AgenticMind provider name -> gen_ai.provider.name well-known value.
GENAI_PROVIDER = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "gcp.gen_ai",
    "azure_openai": "azure.ai.openai",
    "bedrock": "aws.bedrock",
    "groq": "groq",
    "deepseek": "deepseek",
    "mistral": "mistral_ai",
    "xai": "x_ai",
}


@dataclass
class Span:
    trace_id: str  # 32 hex chars
    span_id: str  # 16 hex chars
    parent_id: str | None
    name: str
    start: datetime
    end: datetime
    kind: int = KIND_INTERNAL
    status: int = STATUS_UNSET
    status_message: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, datetime, dict[str, Any]]] = field(default_factory=list)


def _hex(seed: str, size: int) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[: size * 2]


def _content(value: Any, limit: int = 8000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def run_to_spans(
    events: list[TraceEvent],
    *,
    project_name: str = "",
    revision: int | None = None,
    include_content: bool = False,
    labels: dict[str, str] | None = None,
) -> list[Span]:
    """Convert one run's trace events (any order) into spans."""
    if not events:
        return []
    events = sorted(events, key=lambda e: e.seq)
    run_id = events[0].run_id
    trace_id = _hex(f"trace:{run_id}", 16)
    root_id = _hex(f"span:{run_id}:root", 8)
    labels = labels or {}
    first, last = events[0].timestamp, events[-1].timestamp
    root = Span(trace_id, root_id, None, f"invoke_workflow {project_name or events[0].project_id}".strip(), first, last)
    root.attributes.update(
        {
            "gen_ai.operation.name": "invoke_workflow",
            "agenticmind.run.id": run_id,
            "agenticmind.project.id": events[0].project_id,
            **({"agenticmind.project.name": project_name} if project_name else {}),
            **({"agenticmind.project.revision": revision} if revision is not None else {}),
        }
    )
    spans: list[Span] = [root]
    open_nodes: dict[str, Span] = {}
    by_invocation: dict[str, Span] = {}
    tokens_in = tokens_out = 0
    cost = 0.0

    def node_span(e: TraceEvent) -> Span:
        key = e.invocation_id or f"{e.node_id}:{e.seq}"
        span = by_invocation.get(key)
        if span is None:
            span = Span(
                trace_id,
                _hex(f"span:{run_id}:{key}", 8),
                root_id,
                f"node {labels.get(e.node_id or '', e.node_id)}",
                e.timestamp,
                e.timestamp,
            )
            span.attributes.update({"agenticmind.node.id": e.node_id or "", "agenticmind.node.type": e.node_type or ""})
            by_invocation[key] = span
            spans.append(span)
        return span

    for e in events:
        et = e.event_type.value
        if et == "run_started":
            root.start = e.timestamp
        elif et in _TERMINAL_RUN:
            root.end = e.timestamp
            root.attributes["agenticmind.run.status"] = e.status or et.removeprefix("run_")
            if et == "run_failed":
                root.status, root.status_message = STATUS_ERROR, (e.error.message if e.error else "run failed")
                if e.error:
                    root.attributes["error.type"] = e.error.code
            elif et == "run_succeeded":
                root.status = STATUS_OK
        elif et == "node_started":
            span = node_span(e)
            span.start = min(span.start, e.timestamp)
            if e.attempt and e.attempt > 1:
                span.events.append(("retry", e.timestamp, {"attempt": e.attempt}))
            span.attributes["agenticmind.node.attempt"] = e.attempt or 1
            if include_content and e.inputs_summary is not None:
                span.attributes["input.value"] = span.attributes["langfuse.observation.input"] = _content(
                    e.inputs_summary
                )
            open_nodes[e.node_id or ""] = span
        elif et in _NODE_END:
            span = node_span(e)
            span.end = e.timestamp
            if et == "node_skipped":
                span.start = e.timestamp
            span.attributes["agenticmind.node.status"] = e.status or et.removeprefix("node_")
            if et == "node_failed":
                span.status, span.status_message = STATUS_ERROR, (e.error.message if e.error else "node failed")
                if e.error:
                    span.attributes["error.type"] = e.error.code
            elif et == "node_succeeded":
                span.status = STATUS_OK
            if include_content and e.outputs_summary is not None:
                span.attributes["output.value"] = span.attributes["langfuse.observation.output"] = _content(
                    e.outputs_summary
                )
            if e.usage is not None and (e.usage.input_tokens or e.usage.output_tokens or e.usage.model):
                u = e.usage
                span.kind = KIND_CLIENT
                is_agent = (e.node_type or "") in AGENT_NODE_TYPES
                label = labels.get(e.node_id or "", e.node_id or "agent")
                if is_agent:
                    span.name = f"invoke_agent {label}"
                    span.attributes["gen_ai.agent.name"] = label
                elif u.model:
                    span.name = f"chat {u.model}"
                span.attributes.update(
                    {
                        "gen_ai.operation.name": "invoke_agent" if is_agent else "chat",
                        "gen_ai.provider.name": GENAI_PROVIDER.get(u.provider, u.provider),
                        "gen_ai.request.model": u.model,
                        "gen_ai.response.model": u.model,
                        "gen_ai.usage.input_tokens": u.input_tokens,
                        "gen_ai.usage.output_tokens": u.output_tokens,
                        **({"gen_ai.usage.cost": u.estimated_cost_usd} if u.estimated_cost_usd is not None else {}),
                    }
                )
                tokens_in += u.input_tokens
                tokens_out += u.output_tokens
                cost += u.estimated_cost_usd or 0.0
            open_nodes.pop(e.node_id or "", None)
        elif et == "node_event":
            parent = by_invocation.get(e.invocation_id or "") or open_nodes.get(e.node_id or "") or root
            data = dict(e.data)
            kind = str(data.pop("type", "event"))
            if kind == "tool_call":
                duration = float(data.get("duration_ms") or 0)
                tool = str(data.get("tool") or "tool")
                child = Span(
                    trace_id,
                    _hex(f"span:{run_id}:{e.seq}", 8),
                    parent.span_id,
                    f"execute_tool {tool}",
                    e.timestamp - timedelta(milliseconds=duration),
                    e.timestamp,
                )
                child.attributes.update({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool})
                if data.get("agent"):
                    child.attributes["gen_ai.agent.name"] = str(data["agent"])
                child.status = STATUS_OK if data.get("ok") else STATUS_ERROR
                if not data.get("ok"):
                    child.status_message = str(data.get("error", ""))[:500]
                if include_content:
                    child.attributes["gen_ai.tool.call.arguments"] = _content(data.get("arguments"))
                    child.attributes["gen_ai.tool.call.result"] = _content(data.get("result", data.get("error")))
                spans.append(child)
            else:
                attrs = {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))}
                parent.events.append((kind, e.timestamp, attrs))
        elif et == "node_retrying":
            node_span(e).events.append(("retrying", e.timestamp, {"attempt": e.attempt or 0}))
    for span in spans:
        if span.end < span.start:
            span.end = span.start
    root.attributes.update({"gen_ai.usage.input_tokens": tokens_in, "gen_ai.usage.output_tokens": tokens_out})
    if cost:
        root.attributes["agenticmind.run.cost_usd"] = round(cost, 6)
    return spans


# ---- encodings ----------------------------------------------------------------------------------
def _nanos(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000) * 1000


def _json_value(v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _json_attrs(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _json_value(v)} for k, v in attrs.items() if v is not None]


def resource_attributes(service_name: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"service.name": service_name, "telemetry.sdk.name": SCOPE_NAME, **(extra or {})}


def to_otlp_json(spans: list[Span], service_name: str = "agenticmind", resource: dict[str, Any] | None = None) -> dict:
    """An ExportTraceServiceRequest in OTLP/JSON form (hex ids, 64-bit integers as strings, enums as ints)."""
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _json_attrs(resource_attributes(service_name, resource))},
                "scopeSpans": [
                    {
                        "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                        "spans": [
                            {
                                "traceId": s.trace_id,
                                "spanId": s.span_id,
                                **({"parentSpanId": s.parent_id} if s.parent_id else {}),
                                "name": s.name,
                                "kind": s.kind,
                                "startTimeUnixNano": str(_nanos(s.start)),
                                "endTimeUnixNano": str(_nanos(s.end)),
                                "attributes": _json_attrs(s.attributes),
                                "events": [
                                    {"name": n, "timeUnixNano": str(_nanos(t)), "attributes": _json_attrs(a)}
                                    for n, t, a in s.events
                                ],
                                "status": {
                                    "code": s.status,
                                    **({"message": s.status_message} if s.status_message else {}),
                                },
                            }
                            for s in spans
                        ],
                    }
                ],
            }
        ]
    }


def to_otlp_protobuf(
    spans: list[Span], service_name: str = "agenticmind", resource: dict[str, Any] | None = None
) -> bytes:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Status
    from opentelemetry.proto.trace.v1.trace_pb2 import Span as PbSpan

    def kv(attrs: dict[str, Any]) -> list[KeyValue]:
        out = []
        for k, v in attrs.items():
            if v is None:
                continue
            if isinstance(v, bool):
                val = AnyValue(bool_value=v)
            elif isinstance(v, int):
                val = AnyValue(int_value=v)
            elif isinstance(v, float):
                val = AnyValue(double_value=v)
            else:
                val = AnyValue(string_value=str(v))
            out.append(KeyValue(key=k, value=val))
        return out

    pb_spans = []
    for s in spans:
        pb = PbSpan(
            trace_id=bytes.fromhex(s.trace_id),
            span_id=bytes.fromhex(s.span_id),
            parent_span_id=bytes.fromhex(s.parent_id) if s.parent_id else b"",
            name=s.name,
            kind=s.kind,
            start_time_unix_nano=_nanos(s.start),
            end_time_unix_nano=_nanos(s.end),
            attributes=kv(s.attributes),
            status=Status(code=s.status, message=s.status_message),
        )
        for name, t, attrs in s.events:
            pb.events.add(name=name, time_unix_nano=_nanos(t), attributes=kv(attrs))
        pb_spans.append(pb)
    request = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(attributes=kv(resource_attributes(service_name, resource))),
                scope_spans=[
                    ScopeSpans(scope=InstrumentationScope(name=SCOPE_NAME, version=SCOPE_VERSION), spans=pb_spans)
                ],
            )
        ]
    )
    return request.SerializeToString()

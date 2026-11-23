"""OTLP export and metrics from a real engine run (mock LLM + an Agent calling the calculator)."""

from __future__ import annotations

import json

import httpx
import pytest
from agenticmind_ir import load_project
from agenticmind_observability import (
    ExportError,
    OtlpConfig,
    RunMetrics,
    export_spans,
    parse_headers,
    run_to_spans,
    to_otlp_json,
    to_otlp_protobuf,
    traces_url,
)
from agenticmind_registry import default_registry
from agenticmind_runtime import Engine, InMemoryObserver, compile_plan
from agenticmind_runtime.secrets import MappingSecretResolver

MOCK = {"provider": "mock", "model": "mock-1"}
GRAPH = {
    "ir_version": "1.0",
    "project": {"id": "p1", "name": "Traced"},
    "nodes": [
        {
            "id": "q",
            "type": "agenticmind.core.manual_input",
            "version": "1.0.0",
            "config": {"key": "q", "value_type": "String", "default": "calculator: 6*7"},
        },
        {"id": "llm", "type": "agenticmind.models.llm", "version": "1.2.0", "config": MOCK, "ui": {"label": "Writer"}},
        {"id": "calc", "type": "agenticmind.agents.tool_calculator", "version": "1.0.0", "config": {}},
        {"id": "agent", "type": "agenticmind.agents.agent", "version": "1.2.0", "config": MOCK},
        {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "answer"}},
    ],
    "edges": [
        {
            "id": "e1",
            "kind": "data",
            "source": {"node": "q", "port": "value"},
            "target": {"node": "llm", "port": "prompt"},
        },
        {
            "id": "e2",
            "kind": "data",
            "source": {"node": "q", "port": "value"},
            "target": {"node": "agent", "port": "task"},
        },
        {
            "id": "e3",
            "kind": "data",
            "source": {"node": "calc", "port": "tool"},
            "target": {"node": "agent", "port": "tools"},
        },
        {
            "id": "e4",
            "kind": "data",
            "source": {"node": "agent", "port": "answer"},
            "target": {"node": "out", "port": "value"},
        },
    ],
    "metadata": {},
}


@pytest.fixture(scope="module")
def events():
    import asyncio

    plan = compile_plan(load_project(GRAPH), default_registry(), secrets=MappingSecretResolver({}))
    observer = InMemoryObserver()
    result = asyncio.run(Engine(observer=observer, secrets=MappingSecretResolver({})).run(plan, run_id="run_obs1"))
    assert result.status.value == "succeeded"
    return observer.events


def by_name(spans):
    return {s.name: s for s in spans}


def test_run_becomes_a_trace(events):
    spans = run_to_spans(events, project_name="Traced", revision=3, labels={"llm": "Writer"})
    names = by_name(spans)
    root = names["invoke_workflow Traced"]
    assert root.parent_id is None and root.status == 1 and root.attributes["agenticmind.project.revision"] == 3
    chat = names["chat mock-1"]  # the LLM node: a GenAI span
    assert chat.parent_id == root.span_id and chat.kind == 3
    assert chat.attributes["gen_ai.operation.name"] == "chat" and chat.attributes["gen_ai.provider.name"] == "mock"
    assert chat.attributes["gen_ai.usage.input_tokens"] > 0 and chat.attributes["agenticmind.node.id"] == "llm"
    tool = names["execute_tool calculator"]
    agent_span = names["invoke_agent agent"]
    assert (
        agent_span.attributes["gen_ai.operation.name"] == "invoke_agent"
        and agent_span.attributes["gen_ai.agent.name"] == "agent"
    )
    assert tool.parent_id == agent_span.span_id and tool.attributes["gen_ai.tool.name"] == "calculator"
    assert tool.start <= tool.end and tool.status == 1
    assert any(name == "agent_turn" for name, _t, _a in agent_span.events)
    assert "gen_ai.tool.call.arguments" not in tool.attributes and "input.value" not in chat.attributes  # no content
    assert all(s.trace_id == root.trace_id for s in spans)
    assert [s.span_id for s in run_to_spans(events)] == [s.span_id for s in spans]  # deterministic ids
    rich = by_name(run_to_spans(events, include_content=True))
    assert "6*7" in rich["execute_tool calculator"].attributes["gen_ai.tool.call.arguments"]


def test_otlp_encodings(events):
    spans = run_to_spans(events, project_name="Traced")
    body = to_otlp_json(spans, "svc", {"deployment.environment": "test"})
    rs = body["resourceSpans"][0]
    assert {"key": "service.name", "value": {"stringValue": "svc"}} in rs["resource"]["attributes"]
    first = rs["scopeSpans"][0]["spans"][0]
    assert len(first["traceId"]) == 32 and len(first["spanId"]) == 16 and int(first["traceId"], 16) >= 0  # hex
    assert isinstance(first["startTimeUnixNano"], str) and isinstance(first["kind"], int)
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    decoded = ExportTraceServiceRequest.FromString(to_otlp_protobuf(spans, "svc"))
    pb_spans = decoded.resource_spans[0].scope_spans[0].spans
    assert len(pb_spans) == len(spans) and pb_spans[0].trace_id.hex() == spans[0].trace_id


def test_config_from_standard_env():
    assert OtlpConfig.from_env({}) is None
    base = OtlpConfig.from_env(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://cloud.langfuse.com/api/public/otel",
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic%20abc%3D%3D,x-langfuse-ingestion-version=4",
            "OTEL_SERVICE_NAME": "bots",
        }
    )
    assert base.url == "https://cloud.langfuse.com/api/public/otel/v1/traces" and base.service_name == "bots"
    assert base.headers == {"Authorization": "Basic abc==", "x-langfuse-ingestion-version": "4"}
    assert base.protocol == "http/protobuf" and base.public()["header_names"] == [
        "Authorization",
        "x-langfuse-ingestion-version",
    ]
    specific = OtlpConfig.from_env(
        {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318", "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"}
    )
    assert specific.url == "http://collector:4318" and specific.protocol == "http/json"  # used as-is
    with pytest.raises(ExportError, match="grpc"):
        OtlpConfig.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://x:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"})
    assert traces_url("http://x:4318/v1/traces") == "http://x:4318/v1/traces"
    assert parse_headers("a=1, b = two ,junk") == {"a": "1", "b": "two"}


async def test_export_retries_and_content_types(events):
    spans = run_to_spans(events)
    calls = []

    def flaky(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={})

    config = OtlpConfig(url="http://collector:4318/v1/traces", headers={"x-key": "fixture"})
    await export_spans(config, spans, transport=httpx.MockTransport(flaky))
    assert len(calls) == 2 and calls[1].headers["content-type"] == "application/x-protobuf"
    assert calls[1].headers["x-key"] == "fixture"
    seen = []
    await export_spans(
        OtlpConfig(url="http://c/v1/traces", protocol="http/json"),
        spans,
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)),
    )
    assert seen[0].headers["content-type"] == "application/json" and json.loads(seen[0].content)["resourceSpans"]
    bad = []
    with pytest.raises(ExportError, match="HTTP 400"):
        await export_spans(config, spans, transport=httpx.MockTransport(lambda r: bad.append(r) or httpx.Response(400)))
    assert len(bad) == 1  # 400 is not retried


def test_metrics(events):
    m = RunMetrics()
    m.record_run(events)
    m.record_run(events)
    text = m.render({"agenticmind_active_runs": 0})
    assert 'agenticmind_runs_total{status="succeeded"} 2' in text
    assert 'agenticmind_tool_calls_total{tool="calculator",ok="true"} 2' in text
    assert 'agenticmind_model_tokens_total{provider="mock",model="mock-1",direction="input"}' in text
    assert "agenticmind_run_duration_seconds_count 2" in text and "# TYPE agenticmind_runs_total counter" in text

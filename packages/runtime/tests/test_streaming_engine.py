"""The engine routes ``ctx.stream_text`` deltas to observers that take them (ADR-0040)."""

from __future__ import annotations

import asyncio

from agenticmind_ir import load_project
from agenticmind_registry import default_registry
from agenticmind_runtime import Engine, InMemoryObserver, compile_plan
from agenticmind_runtime.secrets import MappingSecretResolver

MOCK = {"provider": "mock", "model": "m"}
GRAPH = {
    "ir_version": "1.0",
    "project": {"id": "p", "name": "Streams"},
    "nodes": [
        {
            "id": "q",
            "type": "agenticmind.core.manual_input",
            "version": "1.0.0",
            "config": {"key": "q", "value_type": "String", "default": "stream these words please"},
        },
        {"id": "llm", "type": "agenticmind.models.llm", "version": "1.2.0", "config": MOCK},
        {"id": "agent", "type": "agenticmind.agents.agent", "version": "1.2.0", "config": MOCK},
        {"id": "a", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "llm"}},
        {"id": "b", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "agent"}},
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
            "source": {"node": "llm", "port": "text"},
            "target": {"node": "a", "port": "value"},
        },
        {
            "id": "e4",
            "kind": "data",
            "source": {"node": "agent", "port": "answer"},
            "target": {"node": "b", "port": "value"},
        },
    ],
    "metadata": {},
}


class StreamingObserver(InMemoryObserver):
    def __init__(self) -> None:
        super().__init__()
        self.deltas: dict[str, list[str]] = {}

    async def on_stream(self, run_id: str, node_id: str, invocation_id: str, delta: str) -> None:
        self.deltas.setdefault(node_id, []).append(delta)


def run(observer):
    plan = compile_plan(load_project(GRAPH), default_registry(), secrets=MappingSecretResolver({}))
    return asyncio.run(Engine(observer=observer, secrets=MappingSecretResolver({})).run(plan))


def test_deltas_reach_streaming_observers_and_add_up():
    obs = StreamingObserver()
    result = run(obs)
    assert result.status.value == "succeeded"
    assert set(obs.deltas) == {"llm", "agent"} and len(obs.deltas["llm"]) > 1
    assert "".join(obs.deltas["llm"]) == result.outputs["llm"]
    assert "".join(obs.deltas["agent"]) == result.outputs["agent"]


def test_plain_observers_run_without_streaming():
    result = run(InMemoryObserver())  # no on_stream: nodes do not stream at all
    assert result.status.value == "succeeded" and result.outputs["llm"]

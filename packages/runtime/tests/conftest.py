from __future__ import annotations

from typing import Any

import pytest
from agenticmind_ir import ProjectDocument, load_project
from agenticmind_node_sdk import (
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    TransientNodeError,
    define_node,
    secret_field,
)
from agenticmind_nodes_core import register
from agenticmind_nodes_data import register as register_data
from agenticmind_nodes_rag import register as register_rag
from agenticmind_registry import NodeRegistry, TrustLevel


class FlakyConfig(NodeConfig):
    fail_times: int = 1


_flaky_calls: dict[str, int] = {}


@define_node(
    type="tests.rt.flaky",
    version="1.0.0",
    display_name="Flaky",
    category="test",
    package="tests",
    outputs=[OutputPort(name="value", type=PortType.STRING)],
    config=FlakyConfig,
    execution=ExecutionPolicy(max_attempts=3),
)
async def flaky(ctx, inputs, config: FlakyConfig) -> NodeResult:
    key = ctx.run_id
    _flaky_calls[key] = _flaky_calls.get(key, 0) + 1
    if _flaky_calls[key] <= config.fail_times:
        raise TransientNodeError("RATE_LIMITED", "try again")
    return NodeResult(outputs={"value": f"ok after {_flaky_calls[key]}"})


@define_node(
    type="tests.rt.boom",
    version="1.0.0",
    display_name="Boom",
    category="test",
    package="tests",
    inputs=[InputPort(name="value", type=PortType.ANY, required=False)],
    outputs=[OutputPort(name="value", type=PortType.STRING)],
)
async def boom(ctx, inputs, config) -> NodeResult:
    raise NodeError("BOOM", "exploded")


@define_node(
    type="tests.rt.bad_output",
    version="1.0.0",
    display_name="Bad output",
    category="test",
    package="tests",
    outputs=[OutputPort(name="value", type=PortType.NUMBER)],
)
async def bad_output(ctx, inputs, config) -> NodeResult:
    return NodeResult(outputs={"value": "not a number"})


class KeyConfig(NodeConfig):
    api_key: SecretRef | None = secret_field()


@define_node(
    type="tests.rt.leaky",
    version="1.0.0",
    display_name="Leaky",
    category="test",
    package="tests",
    outputs=[OutputPort(name="value", type=PortType.STRING)],
    config=KeyConfig,
    permissions=Permissions(credentials=True),
)
async def leaky(ctx, inputs, config: KeyConfig) -> NodeResult:
    key = ctx.secret(config.api_key)
    await ctx.emit("debug", {"note": f"using {key}"})
    return NodeResult(outputs={"value": f"key={key}"})


@define_node(
    type="tests.rt.no_creds",
    version="1.0.0",
    display_name="No creds",
    category="test",
    package="tests",
    outputs=[OutputPort(name="value", type=PortType.STRING)],
    config=KeyConfig,
)
async def no_creds(ctx, inputs, config) -> NodeResult:
    return NodeResult(outputs={"value": "x"})


class AddConfig(NodeConfig):
    amount: float = 1


@define_node(
    type="tests.rt.add",
    version="1.0.0",
    display_name="Add",
    category="test",
    package="tests",
    inputs=[InputPort(name="value", type=PortType.NUMBER)],
    outputs=[OutputPort(name="value", type=PortType.NUMBER)],
    config=AddConfig,
)
async def add(ctx, inputs, config: AddConfig) -> NodeResult:
    return NodeResult(outputs={"value": inputs["value"] + config.amount})


@pytest.fixture
def registry() -> NodeRegistry:
    reg = NodeRegistry()
    register(lambda d: reg.register(d, trust=TrustLevel.CORE, source="agenticmind-nodes-core"))
    register_data(lambda d: reg.register(d, trust=TrustLevel.CORE, source="agenticmind-nodes-data"))
    register_rag(lambda d: reg.register(d, trust=TrustLevel.CORE, source="agenticmind-nodes-rag"))
    for d in (flaky, boom, bad_output, leaky, no_creds, add):
        reg.register(d, trust=TrustLevel.CORE, source="tests")
    return reg


V = "1.0.0"


def n(node_id: str, short_type: str, **config: Any) -> dict[str, Any]:
    t = short_type if short_type.count(".") >= 2 else f"agenticmind.core.{short_type}"
    return {"id": node_id, "type": t, "version": V, "config": config}


def d(edge_id: str, src: str, tgt: str) -> dict[str, Any]:
    (sn, sp), (tn, tp) = src.split("."), tgt.split(".")
    return {"id": edge_id, "kind": "data", "source": {"node": sn, "port": sp}, "target": {"node": tn, "port": tp}}


def c(edge_id: str, src: str, tgt: str, branch: str = "done") -> dict[str, Any]:
    return {
        "id": edge_id,
        "kind": "control",
        "source": {"node": src, "port": branch},
        "target": {"node": tgt, "port": "trigger"},
    }


def doc(nodes: list[dict], edges: list[dict]) -> ProjectDocument:
    return load_project({"ir_version": "1.0", "project": {"id": "p", "name": "P"}, "nodes": nodes, "edges": edges})

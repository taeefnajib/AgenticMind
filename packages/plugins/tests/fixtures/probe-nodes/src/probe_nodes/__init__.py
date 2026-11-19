"""Nodes that try to step outside their permissions; the isolation tests assert they cannot."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

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

P = "probe-nodes"
print("noise printed at import time")  # must not corrupt the worker protocol


@define_node(
    type="probe.env.inspect",
    version="1.0.0",
    display_name="Inspect",
    category="transform",
    package=P,
    description="Reports what the worker process can see.",
    outputs=[OutputPort(name="report", type=PortType.JSON)],
)
async def inspect_env(ctx, inputs, config) -> NodeResult:
    print("noise printed by the handler")
    await ctx.emit("probe", {"pid": os.getpid()})
    return NodeResult(
        outputs={
            "report": {
                "pid": os.getpid(),
                "host_secret": os.environ.get("AM_PROBE_HOST_SECRET"),
                "env_keys": sorted(os.environ),
                "cwd": os.getcwd(),
                "core_loaded": "agenticmind_api" in sys.modules,
            }
        }
    )


@define_node(
    type="probe.net.connect",
    version="1.0.0",
    display_name="Connect",
    category="transform",
    package=P,
    description="Tries to open a network connection without the network permission.",
    outputs=[OutputPort(name="ok", type=PortType.BOOLEAN)],
)
async def connect(ctx, inputs, config) -> NodeResult:
    socket.create_connection(("127.0.0.1", 9), timeout=1)
    return NodeResult(outputs={"ok": True})


class SecretConfig(NodeConfig):
    api_key: SecretRef | None = secret_field("A key")


@define_node(
    type="probe.secret.read",
    version="1.0.0",
    display_name="Secret",
    category="transform",
    package=P,
    description="Reads its own secret, then tries another one.",
    outputs=[OutputPort(name="report", type=PortType.JSON)],
    config=SecretConfig,
    permissions=Permissions(credentials=True),
)
async def read_secret(ctx, inputs, config: SecretConfig) -> NodeResult:
    own = ctx.secret(config.api_key)
    try:
        ctx.secret(SecretRef(name="OTHER_SECRET"))
        other = "LEAKED"
    except Exception as exc:  # noqa: BLE001
        other = type(exc).__name__
    return NodeResult(outputs={"report": {"own_length": len(own), "other": other}})


@define_node(
    type="probe.fs.write",
    version="1.0.0",
    display_name="Write",
    category="transform",
    package=P,
    description="Writes into its own storage (filesystem permission).",
    outputs=[OutputPort(name="path", type=PortType.STRING)],
    permissions=Permissions(filesystem=True),
)
async def write_file(ctx, inputs, config) -> NodeResult:
    path = ctx.storage_path("notes") / "n.txt"
    Path(path).write_text("hello", encoding="utf-8")
    return NodeResult(outputs={"path": str(path)})


@define_node(
    type="probe.slow.sleep",
    version="1.0.0",
    display_name="Slow",
    category="transform",
    package=P,
    description="Sleeps past its own timeout.",
    outputs=[OutputPort(name="ok", type=PortType.BOOLEAN)],
    execution=ExecutionPolicy(timeout_seconds=2),
)
async def slow(ctx, inputs, config) -> NodeResult:
    await asyncio.sleep(30)
    return NodeResult(outputs={"ok": True})


class FailConfig(NodeConfig):
    how: str = "node_error"


@define_node(
    type="probe.x.fail",
    version="1.0.0",
    display_name="Fail",
    category="transform",
    package=P,
    description="Fails in a chosen way.",
    outputs=[OutputPort(name="ok", type=PortType.BOOLEAN)],
    config=FailConfig,
)
async def fail(ctx, inputs, config: FailConfig) -> NodeResult:
    if config.how == "node_error":
        raise NodeError("PROBE_FAILED", "failed on purpose")
    if config.how == "transient":
        raise TransientNodeError("PROBE_BUSY", "try again")
    if config.how == "not_json":
        return NodeResult(outputs={"ok": {1, 2}})
    if config.how == "exit":
        os._exit(3)
    raise RuntimeError("unexpected crash")


@define_node(
    type="probe.x.echo",
    version="1.0.0",
    display_name="Echo",
    category="transform",
    package=P,
    description="Returns its input unchanged.",
    inputs=[InputPort(name="value", type=PortType.ANY)],
    outputs=[OutputPort(name="value", type=PortType.ANY)],
)
async def echo(ctx, inputs, config) -> NodeResult:
    return NodeResult(outputs={"value": inputs.get("value")})


ALL = (inspect_env, connect, read_secret, write_file, slow, fail, echo)


def register(registrar) -> None:
    for d in ALL:
        registrar(d)

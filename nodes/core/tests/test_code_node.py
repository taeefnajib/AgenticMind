"""Code node: real executions with the process backend; Docker absence reported clearly."""

from __future__ import annotations

import importlib
import shutil

import pytest
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node

mod = importlib.import_module("agenticmind_nodes_core.code")


@pytest.fixture
def process_mode(monkeypatch):
    monkeypatch.setenv("AGENTICMIND_SANDBOX", "process")


async def test_default_example_runs(process_mode):
    r = await invoke_node(mod.code, inputs={"inputs": [2, 4, 6]})
    assert r.outputs["result"] == {"count": 3, "mean": 4.0, "max": 6}
    assert r.outputs["error"] is None and r.outputs["report"]["backend"] == "process"
    without_input = await invoke_node(mod.code)  # falls back to the example's own numbers
    assert without_input.outputs["result"]["count"] == 6


async def test_errors_fail_or_are_output(process_mode):
    with pytest.raises(NodeError) as exc:
        await invoke_node(mod.code, config={"code": "print('step 1')\n1/0"})
    assert (
        exc.value.code == "CODE_FAILED" and "ZeroDivisionError" in exc.value.message and "line 2" in exc.value.message
    )
    soft = await invoke_node(mod.code, config={"code": "print('step 1')\n1/0", "on_error": "output"})
    assert soft.outputs["result"] is None and soft.outputs["stdout"] == "step 1"
    assert soft.outputs["error"]["type"] == "ZeroDivisionError"
    slow = await invoke_node(mod.code, config={"code": "while True: pass", "timeout_seconds": 1, "on_error": "output"})
    assert slow.outputs["error"]["type"] == "Timeout" and slow.outputs["report"]["timed_out"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_javascript(process_mode):
    cfg = {"language": "javascript", "code": "return `Hi ${inputs.name}`.toUpperCase();"}
    r = await invoke_node(mod.code, inputs={"inputs": {"name": "Ada"}}, config=cfg)
    assert r.outputs["result"] == "HI ADA"


async def test_sandbox_unavailable_and_disabled(monkeypatch):
    monkeypatch.setenv("AGENTICMIND_SANDBOX", "docker")
    monkeypatch.setattr("agenticmind_sandbox.runner.shutil.which", lambda name: None)
    with pytest.raises(NodeError) as exc:
        await invoke_node(mod.code)
    assert exc.value.code == "SANDBOX_UNAVAILABLE" and "AGENTICMIND_SANDBOX=process" in exc.value.message
    monkeypatch.setenv("AGENTICMIND_SANDBOX", "off")
    with pytest.raises(NodeError) as exc:
        await invoke_node(mod.code)
    assert exc.value.code == "SANDBOX_DISABLED"
    monkeypatch.setenv("AGENTICMIND_SANDBOX", "process")
    with pytest.raises(NodeError) as exc:
        await invoke_node(mod.code, config={"network": True})
    assert exc.value.code == "SANDBOX_NETWORK_NOT_ALLOWED"

"""Code Interpreter Tool: an agent fixes its code after seeing the traceback (process backend)."""

from __future__ import annotations

import importlib

import pytest
from agenticmind_node_sdk import ModelUsage, NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_providers import ChatResponse, ToolCall

ct = importlib.import_module("agenticmind_nodes_agents.code_tool")
agent_mod = importlib.import_module("agenticmind_nodes_agents.agent")
FIXED = "print('ok')\nresult = sum(inputs) / len(inputs)"
USAGE = ModelUsage(provider="x", model="m", input_tokens=1, output_tokens=1)


async def test_agent_runs_code_and_recovers_from_an_error(monkeypatch):
    monkeypatch.setenv("AGENTICMIND_SANDBOX", "process")
    tool = (await invoke_node(ct.code_tool)).outputs["tool"]
    assert tool["permissions"] == ["subprocess"] and "Python 3" in tool["description"]
    seen = []

    class Scripted:
        async def chat(self, request, *, api_key=None, base_url=None):
            seen.append(request)
            if len(seen) == 1:
                return ChatResponse(text="", usage=USAGE, tool_calls=[
                    ToolCall(id="1", name="run_code", arguments={"code": "result = sum(inputs) / len(inputz)",
                                                                  "inputs": [1, 2, 3, 4]})])  # fmt: skip
            if len(seen) == 2:
                assert "NameError" in seen[-1].messages[-1].content  # the traceback reached the model
                return ChatResponse(text="", usage=USAGE, tool_calls=[
                    ToolCall(id="2", name="run_code", arguments={"code": FIXED, "inputs": [1, 2, 3, 4]})])  # fmt: skip
            return ChatResponse(text="The mean is 2.5.", usage=USAGE)

    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Scripted())
    r = await invoke_node(agent_mod.agent, inputs={"task": "mean of 1..4", "tools": [tool]},
                          config={"provider": "mock", "model": "m"})  # fmt: skip
    steps = r.outputs["result"]["steps"]
    assert [s["ok"] for s in steps] == [False, True] and "2.5" in steps[1]["result_summary"]
    assert r.outputs["answer"] == "The mean is 2.5."


async def test_older_agents_cannot_run_code():
    tool = (await invoke_node(ct.code_tool)).outputs["tool"]
    with pytest.raises(NodeError) as exc:
        await invoke_node(agent_mod.agent_v11, inputs={"task": "x", "tools": [tool]},
                          config={"provider": "mock", "model": "m"})  # fmt: skip
    assert exc.value.code == "TOOL_PERMISSION_DENIED"

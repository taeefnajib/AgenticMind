"""MCP client nodes against a real stdio MCP server (a fixture script run with this Python)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from agenticmind_node_sdk import ModelUsage, NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_providers import ChatResponse, ToolCall

mc = importlib.import_module("agenticmind_nodes_agents.mcp_client")
agent_mod = importlib.import_module("agenticmind_nodes_agents.agent")
flow = importlib.import_module("agenticmind_nodes_agents.tool_flow")

SERVER = str(Path(__file__).parent / "fixtures" / "mcp_demo_server.py")
STDIO = {"transport": "stdio", "command": sys.executable, "args": [SERVER]}
USAGE = ModelUsage(provider="x", model="m", input_tokens=1, output_tokens=1)


async def toolkit(**config):
    return await invoke_node(mc.mcp_tools, config={**STDIO, **config}, secrets={"DEMO_TOKEN": "fixture-env-value"})


async def test_mcp_server_node_lists_tools_as_a_toolkit():
    r = await toolkit(name_prefix="demo")
    names = [t["name"] for t in r.outputs["catalog"]]
    assert names == ["demo_add", "demo_word_count", "demo_read_env", "demo_explode"]  # 'word.count' made safe
    assert r.outputs["catalog"][1]["remote"] == "word.count"
    handle = r.outputs["tools"]
    assert handle["tool_type"] == "mcp" and handle["permissions"] == ["subprocess", "credentials"]
    assert handle["binding"]["server"]["command"] == sys.executable
    add = next(t for t in r.outputs["catalog"] if t["name"] == "demo_add")
    assert set(add["parameters"]["properties"]) == {"a", "b"}
    only = await toolkit(include_tools=["add"], exclude_tools=["explode"])
    assert [t["name"] for t in only.outputs["catalog"]] == ["add"]
    with pytest.raises(NodeError) as exc:
        await toolkit(include_tools=["nope"])
    assert exc.value.code == "MCP_TOOL_UNKNOWN"


async def test_agent_uses_mcp_tools_and_sees_tool_errors(monkeypatch):
    kit = (await toolkit(name_prefix="demo")).outputs["tools"]
    seen = []

    class Scripted:
        async def chat(self, request, *, api_key=None, base_url=None):
            seen.append(request)
            if len(seen) == 1:
                assert {t.name for t in request.tools} == {"demo_add", "demo_word_count", "demo_read_env",
                                                           "demo_explode"}  # fmt: skip
                return ChatResponse(text="", usage=USAGE, tool_calls=[
                    ToolCall(id="1", name="demo_add", arguments={"a": 2, "b": 40}),
                    ToolCall(id="2", name="demo_explode", arguments={}),
                ])  # fmt: skip
            return ChatResponse(text="The sum is 42.", usage=USAGE)

    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Scripted())
    r = await invoke_node(agent_mod.agent, inputs={"task": "add 2 and 40", "tools": [kit]},
                          config={"provider": "mock", "model": "m"})  # fmt: skip
    assert r.outputs["answer"] == "The sum is 42."
    steps = {s["tool"]: s for s in r.outputs["result"]["steps"]}
    assert steps["demo_add"]["ok"] and steps["demo_add"]["result_summary"] == "42.0"
    assert not steps["demo_explode"]["ok"] and "kaboom" in steps["demo_explode"]["error"]


async def test_older_agent_versions_refuse_local_process_tools():
    kit = (await toolkit()).outputs["tools"]
    with pytest.raises(NodeError) as exc:
        await invoke_node(agent_mod.agent_v11, inputs={"task": "x", "tools": [kit]},
                          config={"provider": "mock", "model": "m"})  # fmt: skip
    assert exc.value.code == "TOOL_PERMISSION_DENIED" and "subprocess" in exc.value.message


async def test_mcp_call_node_with_secret_env():
    cfg = {**STDIO, "tool": "read_env", "arguments": {"name": "DEMO_TOKEN"},
           "secret_env": {"DEMO_TOKEN": {"$secret": "DEMO_TOKEN"}}}  # fmt: skip
    r = await invoke_node(mc.mcp_call, config=cfg, secrets={"DEMO_TOKEN": "fixture-env-value"})
    assert r.outputs["result"] == "fixture-env-value" and r.outputs["is_error"] is False
    # the parent's environment is not inherited: an unrelated variable is invisible to the server
    monkey = await invoke_node(mc.mcp_call, config={**STDIO, "tool": "read_env"},
                               inputs={"arguments": {"name": "OPENROUTER_API_KEY"}})  # fmt: skip
    assert monkey.outputs["result"] == ""
    counted = await invoke_node(mc.mcp_call, config={**STDIO, "tool": "word.count"},
                                inputs={"arguments": {"text": "one two three"}})  # fmt: skip
    assert counted.outputs["result"] == {"words": 3}
    with pytest.raises(NodeError) as exc:
        await invoke_node(mc.mcp_call, config={**STDIO, "tool": "explode"})
    assert exc.value.code == "MCP_TOOL_ERROR" and "kaboom" in exc.value.message
    soft = await invoke_node(mc.mcp_call, config={**STDIO, "tool": "explode", "fail_on_tool_error": False})
    assert soft.outputs["is_error"] is True


async def test_tool_router_v11_runs_mcp_tools():
    kit = (await toolkit(include_tools=["add"])).outputs["tools"]
    cfg = {"provider": "mock", "model": "m", "strategy": "keywords", "keywords": {"add": ["add"]}}
    r = await invoke_node(flow.tool_router, inputs={"query": "please add these", "tools": [kit]}, config=cfg)
    assert r.outputs["tool"] == "add"


async def test_connection_errors_and_config_validation():
    with pytest.raises(NodeError) as exc:
        await invoke_node(mc.mcp_tools, config={"transport": "stdio", "command": "definitely-not-a-command-xyz"})
    assert exc.value.code == "MCP_CONNECTION_FAILED"
    with pytest.raises(Exception, match="needs a command"):
        mc.McpToolsConfig.model_validate({"transport": "stdio"})
    with pytest.raises(Exception, match="link-local"):
        mc.McpToolsConfig.model_validate({"transport": "http", "url": "http://169.254.169.254/mcp"})
    mc.McpToolsConfig.model_validate({"transport": "http", "url": "http://localhost:8000/api/mcp"})  # local is fine
    assert mc.tool_name("gh", "create.issue") == "gh_create_issue" and mc.tool_name("", "9lives") == "t_9lives"

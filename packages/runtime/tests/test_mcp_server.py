"""Graphs exposed as MCP tools: schema from Manual Inputs, the in-memory server, and ``agenticmind mcp-serve``."""

from __future__ import annotations

import importlib
import json
import sys

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_runtime.mcp_server import (
    ExposedGraph,
    RunReply,
    build_mcp_server,
    exposure,
    graph_input_schema,
    mcp_tool_name,
)

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

GREETER = {
    "ir_version": "1.0",
    "project": {"id": "p1", "name": "Greeter Bot!", "description": "Greets someone by name."},
    "nodes": [
        {"id": "name", "type": "agenticmind.core.manual_input", "version": "1.0.0",
         "config": {"key": "name", "value_type": "String", "required": True}, "ui": {"label": "Person's name"}},
        {"id": "times", "type": "agenticmind.core.manual_input", "version": "1.0.0",
         "config": {"key": "times", "value_type": "Number", "default": 1}},
        {"id": "hello", "type": "agenticmind.core.text_template", "version": "1.0.0",
         "config": {"template": "Hello {{ text }}!", "strict": True}},
        {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "greeting"}},
    ],
    "edges": [
        {"id": "e1", "kind": "data", "source": {"node": "name", "port": "value"},
         "target": {"node": "hello", "port": "text"}},
        {"id": "e2", "kind": "data", "source": {"node": "hello", "port": "text"},
         "target": {"node": "out", "port": "value"}},
    ],
    "metadata": {"mcp": {"expose": True}},
}  # fmt: skip


def test_input_schema_and_names():
    doc = load_project(GREETER)
    schema = graph_input_schema(doc)
    assert schema["required"] == ["name"] and schema["additionalProperties"] is False
    assert schema["properties"]["name"] == {"type": "string", "description": "Person's name"}
    assert schema["properties"]["times"] == {"type": "number", "description": "times", "default": 1}
    assert mcp_tool_name("Greeter Bot!") == "greeter_bot" and mcp_tool_name("  ") == "graph"
    assert exposure(doc) == {"expose": True}
    assert exposure(load_project({**GREETER, "metadata": {}})) is None


async def test_server_lists_validates_and_runs():
    calls = []

    async def run(inputs):
        calls.append(inputs)
        if inputs["name"] == "boom":
            return RunReply(status="failed", error={"message": "node exploded"})
        return RunReply(status="succeeded", outputs={"greeting": f"Hello {inputs['name']}!"})

    schema = graph_input_schema(load_project(GREETER))

    async def graphs():
        return [ExposedGraph("greeter_bot", "Greets someone", schema, run)]

    async with Client(build_mcp_server(graphs)) as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == ["greeter_bot"] and tools[0].input_schema["required"] == ["name"]
        ok = await client.call_tool("greeter_bot", {"name": "Ada"})
        assert not ok.is_error and ok.structured_content == {"greeting": "Hello Ada!"}
        assert ok.content[0].text == "Hello Ada!"
        bad = await client.call_tool("greeter_bot", {"name": 5})
        assert bad.is_error and "Invalid arguments" in bad.content[0].text and len(calls) == 1
        failed = await client.call_tool("greeter_bot", {"name": "boom"})
        assert failed.is_error and "node exploded" in failed.content[0].text
        unknown = await client.call_tool("nope", {})
        assert unknown.is_error


async def test_cli_mcp_serve_over_stdio(tmp_path):
    graph = tmp_path / "greeter.json"
    graph.write_text(json.dumps(GREETER), encoding="utf-8")
    mc = importlib.import_module("agenticmind_nodes_agents.mcp_client")
    server = {"transport": "stdio", "command": sys.executable,
              "args": ["-m", "agenticmind_runtime.cli", "mcp-serve", str(graph), "--data-dir", str(tmp_path)],
              "env": {"PYTHONIOENCODING": "utf-8"}}  # fmt: skip
    listed = await invoke_node(mc.mcp_tools, config=server)
    assert [t["name"] for t in listed.outputs["catalog"]] == ["greeter_bot"]
    r = await invoke_node(mc.mcp_call, config={**server, "tool": "greeter_bot"}, inputs={"arguments": {"name": "Ada"}})
    assert r.outputs["result"] == {"greeting": "Hello Ada!"}
    with pytest.raises(NodeError) as exc:
        await invoke_node(mc.mcp_call, config={**server, "tool": "greeter_bot", "arguments": {"name": 1}})
    assert exc.value.code == "MCP_TOOL_ERROR" and "Invalid arguments" in exc.value.message

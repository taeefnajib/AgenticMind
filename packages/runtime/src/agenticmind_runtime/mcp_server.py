"""Expose AgenticMind graphs as Model Context Protocol tools (ADR-0033).

Each exposed graph becomes one MCP tool. Its input schema comes from the graph's Manual Input nodes (key,
value type, default, required); calling the tool runs the graph and returns its outputs as structured
content. The same builder serves stdio (``agenticmind mcp-serve graph.json``) and Streamable HTTP (the API's
``/api/mcp`` endpoint).

The ``mcp`` package is an optional dependency (``agenticmind-runtime[mcp]``); it is imported lazily.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from agenticmind_ir import ProjectDocument

MANUAL_INPUT = "agenticmind.core.manual_input"
_JSON_TYPES = {"String": "string", "Number": "number", "Boolean": "boolean"}


@dataclass
class RunReply:
    status: str
    outputs: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    run_id: str | None = None


@dataclass
class ExposedGraph:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[RunReply]]


def mcp_tool_name(raw: str) -> str:
    """A valid MCP tool name (letters, digits, ``_``, ``-``; at most 64 characters)."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", raw.strip().lower()).strip("_-") or "graph"
    return name[:64]


def graph_input_schema(doc: ProjectDocument) -> dict[str, Any]:
    """JSON Schema for a graph's run input, from its Manual Input nodes."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for node in doc.nodes:
        if node.type != MANUAL_INPUT:
            continue
        cfg = node.config or {}
        key = str(cfg.get("key") or "input")
        prop: dict[str, Any] = {}
        json_type = _JSON_TYPES.get(str(cfg.get("value_type") or "String"))
        if json_type:
            prop["type"] = json_type
        label = node.ui.label if node.ui and node.ui.label else None
        prop["description"] = label or key
        if cfg.get("default") is not None:
            prop["default"] = cfg["default"]
        elif cfg.get("required", True):
            required.append(key)
        properties[key] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = sorted(set(required))
    return schema


def describe(doc: ProjectDocument, override: str | None = None) -> str:
    text = (override or doc.project.description or doc.project.name or "An AgenticMind workflow").strip()
    return text[:2000]


def exposure(doc: ProjectDocument) -> dict[str, Any] | None:
    """The project's MCP settings (``metadata.mcp``) when it is exposed, else None."""
    meta = doc.metadata.get("mcp") if isinstance(doc.metadata, dict) else None
    if isinstance(meta, dict) and meta.get("expose") is True:
        return meta
    return None


def build_mcp_server(
    list_graphs: Callable[[], Awaitable[list[ExposedGraph]]], *, name: str = "AgenticMind", version: str = "0.1.0"
) -> Any:
    """A low-level ``mcp.server.Server`` whose tools are the graphs returned by ``list_graphs``."""
    from jsonschema import Draft202012Validator
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    async def by_name() -> dict[str, ExposedGraph]:
        graphs: dict[str, ExposedGraph] = {}
        for g in await list_graphs():
            graphs.setdefault(g.name, g)  # first wins on a name clash
        return graphs

    def error(text: str) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)

    async def on_list_tools(ctx: Any, params: Any) -> ListToolsResult:
        graphs = await by_name()
        return ListToolsResult(
            tools=[Tool(name=g.name, description=g.description, input_schema=g.input_schema) for g in graphs.values()]
        )

    async def on_call_tool(ctx: Any, params: Any) -> CallToolResult:
        graph = (await by_name()).get(params.name)
        if graph is None:
            return error(f"Unknown tool '{params.name}'")
        args = dict(params.arguments or {})
        problems = [
            f"{'/'.join(str(p) for p in e.absolute_path) or 'arguments'}: {e.message}"
            for e in Draft202012Validator(graph.input_schema).iter_errors(args)
        ]
        if problems:
            return error("Invalid arguments: " + "; ".join(problems[:5]))
        reply = await graph.run(args)
        if reply.status != "succeeded":
            detail = (reply.error or {}).get("message") or reply.status
            return error(f"The workflow did not succeed ({reply.status}): {detail}")
        outputs = json.loads(json.dumps(reply.outputs, default=str))
        if len(outputs) == 1:
            only = next(iter(outputs.values()))
            text = only if isinstance(only, str) else json.dumps(only, ensure_ascii=False, indent=2)
        else:
            text = json.dumps(outputs, ensure_ascii=False, indent=2)
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=outputs)

    return Server(name, version=version, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def serve_stdio(server: Any) -> None:
    from mcp import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

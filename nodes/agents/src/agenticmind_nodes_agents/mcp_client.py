"""MCP client nodes (ADR-0033): connect to Model Context Protocol servers and use their tools.

* **MCP Server** (``agenticmind.agents.mcp_tools``) connects to a server (a local command over stdio, or a URL
  over Streamable HTTP), lists its tools and outputs them as one *toolkit* tool value. Wired into an Agent (or
  Tool Router / Tool Chain) the toolkit expands into one tool per MCP tool.
* **MCP Call** (``agenticmind.agents.mcp_call``) calls one MCP tool directly, without a model.

Connections are opened per call and closed afterwards (simple and leak-free; a local stdio server is started
for each call). Secrets are carried as references in the tool binding and resolved only when a call runs.
The stdio child process inherits only a small allow-list of environment variables (PATH, home/temp
locations), plus the variables configured here: the server's own API keys never leak into it.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlparse

from agenticmind_node_sdk import (
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    ToolHandle,
    TransientNodeError,
    define_node,
    secret_field,
    ui,
)
from agenticmind_node_sdk.handles import handle_value
from pydantic import Field, model_validator

from .tools import PACKAGE, ToolError, implementation

_STDIO = {"transport": ["stdio"]}
_HTTP = {"transport": ["http"]}
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


class McpConnection(NodeConfig):
    transport: Literal["stdio", "http"] = Field(
        default="stdio", description="stdio: start a local command. http: connect to a Streamable HTTP URL."
    )
    command: str = Field(
        default="", description="Executable, e.g. npx, uvx, python or docker.",
        json_schema_extra=ui(show_when=_STDIO, placeholder="npx"),
    )  # fmt: skip
    args: list[str] = Field(
        default_factory=list, description='Arguments, e.g. ["-y", "@modelcontextprotocol/server-everything"].',
        json_schema_extra=ui(show_when=_STDIO),
    )  # fmt: skip
    env: dict[str, str] = Field(
        default_factory=dict, description="Extra environment variables for the command (non-secret).",
        json_schema_extra=ui(show_when=_STDIO, advanced=True),
    )  # fmt: skip
    secret_env: dict[str, SecretRef] = Field(
        default_factory=dict,
        description='Environment variables filled from secrets, e.g. {"GITHUB_TOKEN": {"$secret": "GITHUB_TOKEN"}}.',
        json_schema_extra=ui(show_when=_STDIO),
    )
    cwd: str | None = Field(default=None, json_schema_extra=ui(show_when=_STDIO, advanced=True))
    url: str = Field(
        default="", description="Streamable HTTP endpoint, e.g. http://localhost:8000/mcp.",
        json_schema_extra=ui(show_when=_HTTP, placeholder="https://example.com/mcp"),
    )  # fmt: skip
    bearer_token: SecretRef | None = secret_field("Sent as 'Authorization: Bearer …'", show_when=_HTTP)
    headers: dict[str, str] = Field(
        default_factory=dict, description="Extra HTTP headers (non-secret).",
        json_schema_extra=ui(show_when=_HTTP, advanced=True),
    )  # fmt: skip
    timeout_seconds: float = Field(default=60, ge=1, le=1800, description="Per tool call.",
                                   json_schema_extra=ui(advanced=True))  # fmt: skip

    @model_validator(mode="after")
    def _check(self) -> McpConnection:
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio transport needs a command")
        if self.transport == "http":
            parsed = urlparse(self.url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("http transport needs an http(s) URL")
            _check_host(parsed.hostname)
        for name in [*self.env, *self.secret_env]:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"invalid environment variable name {name!r}")
        return self

    def binding(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def permissions(self) -> list[str]:
        return ["subprocess", "credentials"] if self.transport == "stdio" else ["network", "credentials"]


def _check_host(host: str) -> None:
    """Local MCP servers are normal (localhost is allowed); cloud metadata endpoints never are."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        if host.lower() in ("metadata.google.internal", "metadata"):
            raise ValueError("cloud metadata endpoints are not allowed") from None
        return
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError(f"address {ip} is not allowed (link-local / metadata range)")


@asynccontextmanager
async def open_client(ctx: NodeContext, binding: dict[str, Any]) -> AsyncIterator[Any]:
    """An initialised ``mcp.Client`` for a connection binding (secrets resolved here, never stored)."""
    from mcp import Client, StdioServerParameters

    conn = McpConnection.model_validate(binding)
    if conn.transport == "stdio":
        from mcp.client.stdio import get_default_environment

        env = {**get_default_environment(), **conn.env}
        env.update({k: ctx.secret(ref) for k, ref in conn.secret_env.items()})
        params = StdioServerParameters(command=conn.command, args=conn.args, env=env, cwd=conn.cwd)
        async with Client(params, read_timeout_seconds=conn.timeout_seconds) as client:
            yield client
        return
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    headers = dict(conn.headers)
    if conn.bearer_token is not None:
        headers["Authorization"] = f"Bearer {ctx.secret(conn.bearer_token)}"
    async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=conn.timeout_seconds)) as http:
        transport = streamable_http_client(conn.url, http_client=http)
        async with Client(transport, read_timeout_seconds=conn.timeout_seconds) as client:
            yield client


def _connection_error(exc: BaseException, conn: dict[str, Any]) -> NodeError:
    where = conn.get("url") or " ".join([conn.get("command", ""), *conn.get("args", [])]).strip()
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        detail = "; ".join(f"{type(e).__name__}: {e}" for e in exc.exceptions[:3])
    return TransientNodeError("MCP_CONNECTION_FAILED", f"Could not use MCP server ({where}): {detail}")


async def list_remote_tools(ctx: NodeContext, binding: dict[str, Any]) -> list[Any]:
    tools: list[Any] = []
    try:
        async with open_client(ctx, binding) as client:
            cursor: str | None = None
            for _ in range(50):  # pagination guard
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
    except NodeError:
        raise
    except (Exception, BaseExceptionGroup) as exc:  # noqa: BLE001 - transport errors vary by transport
        raise _connection_error(exc, binding) from exc
    return tools


def result_value(result: Any) -> tuple[Any, str]:
    """(value for the model / outputs, plain text) from a CallToolResult."""
    from mcp.types import EmbeddedResource, ImageContent, ResourceLink, TextContent

    texts: list[str] = []
    for block in result.content or []:
        if isinstance(block, TextContent):
            texts.append(block.text)
        elif isinstance(block, ImageContent):
            texts.append(f"[image {block.mime_type}, {len(block.data) * 3 // 4} bytes]")
        elif isinstance(block, ResourceLink):
            texts.append(f"[resource {block.uri}]")
        elif isinstance(block, EmbeddedResource):
            res = block.resource
            texts.append(getattr(res, "text", None) or f"[resource {res.uri}]")
        else:
            texts.append(f"[{getattr(block, 'type', 'content')}]")
    text = "\n".join(texts)
    structured = result.structured_content
    if structured is not None:
        # SDK servers wrap a plain return value as {"result": value}; unwrap when the text says the same thing
        if isinstance(structured, dict) and list(structured) == ["result"]:
            inner = structured["result"]
            if text in (str(inner), json.dumps(inner, ensure_ascii=False), json.dumps(inner, ensure_ascii=False,
                                                                                       indent=2)):  # fmt: skip
                return inner, text
        return structured, text or json.dumps(structured, ensure_ascii=False)
    if len(texts) == 1:
        try:
            return json.loads(texts[0]), text
        except (json.JSONDecodeError, TypeError):
            pass
    return text, text


async def call_remote_tool(ctx: NodeContext, binding: dict[str, Any], name: str, arguments: dict[str, Any]) -> Any:
    try:
        async with open_client(ctx, binding) as client:
            result = await client.call_tool(name, arguments)
    except NodeError:
        raise
    except (Exception, BaseExceptionGroup) as exc:  # noqa: BLE001
        raise _connection_error(exc, binding) from exc
    return result


def tool_name(prefix: str, remote: str) -> str:
    base = _TOOL_NAME_RE.sub("_", remote).strip("_") or "tool"
    name = f"{prefix}_{base}" if prefix else base
    if not re.match(r"^[A-Za-z]", name):
        name = f"t_{name}"
    return name[:64]


# ---- MCP Server (toolkit) -------------------------------------------------------------------------
class McpToolsConfig(McpConnection):
    name_prefix: str = Field(
        default="", pattern=r"^([A-Za-z][A-Za-z0-9_]{0,23})?$",
        description="Prepended to tool names (e.g. 'github' → github_create_issue); avoids clashes.",
    )  # fmt: skip
    include_tools: list[str] = Field(default_factory=list, description="Only these tools (empty = all).")
    exclude_tools: list[str] = Field(default_factory=list, description="Never these tools.",
                                     json_schema_extra=ui(advanced=True))  # fmt: skip


@define_node(
    type="agenticmind.agents.mcp_tools",
    version="1.0.0",
    display_name="MCP Server",
    category="tools",
    package=PACKAGE,
    description="Connects to a Model Context Protocol server and gives an agent all of its tools.",
    outputs=[
        OutputPort(name="tools", type=PortType.TOOL, description="The server's tools (connect to an Agent)"),
        OutputPort(name="catalog", type=PortType.JSON, description="Name, description and input schema per tool"),
    ],
    config=McpToolsConfig,
    permissions=Permissions(network=True, subprocess=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["tools", "mcp"],
)
async def mcp_tools(ctx: NodeContext, inputs, config: McpToolsConfig) -> NodeResult:
    binding = McpConnection.model_validate(config.model_dump(include=set(McpConnection.model_fields))).binding()
    remote = await list_remote_tools(ctx, binding)
    include, exclude = set(config.include_tools), set(config.exclude_tools)
    unknown = include - {t.name for t in remote}
    if unknown:
        raise NodeError("MCP_TOOL_UNKNOWN", f"The server has no tool(s): {', '.join(sorted(unknown))}")
    toolkit = []
    seen: set[str] = set()
    for t in remote:
        if (include and t.name not in include) or t.name in exclude:
            continue
        local = tool_name(config.name_prefix, t.name)
        if local in seen:
            raise NodeError("DUPLICATE_TOOL_NAME", f"Two MCP tools map to the name '{local}'")
        seen.add(local)
        schema = t.input_schema if isinstance(t.input_schema, dict) else {"type": "object"}
        toolkit.append({
            "name": local,
            "remote": t.name,
            "description": (t.description or t.title or t.name).strip()[:2000],
            "parameters": schema or {"type": "object"},
        })  # fmt: skip
    if not toolkit:
        raise NodeError("MCP_NO_TOOLS", "The MCP server exposes no tools (after include/exclude filters)")
    prefix = config.name_prefix or "mcp"
    handle = ToolHandle(
        tool_type="mcp",
        name=tool_name("", prefix),
        description=f"Toolkit of {len(toolkit)} MCP tools: " + ", ".join(t["name"] for t in toolkit)[:1900],
        parameters={"type": "object"},
        binding={"server": binding, "toolkit": toolkit},
        permissions=McpConnection.model_validate(binding).permissions(),
    )
    await ctx.emit("mcp_tools_listed", {"count": len(toolkit), "tools": [t["name"] for t in toolkit][:50]})
    catalog = [{"name": t["name"], "remote": t["remote"], "description": t["description"],
                "parameters": t["parameters"]} for t in toolkit]  # fmt: skip
    return NodeResult(outputs={"tools": handle_value(handle), "catalog": catalog})


def expand_toolkit(handle: ToolHandle) -> list[ToolHandle]:
    """One tool per MCP tool in a toolkit handle (the Agent sees each tool separately)."""
    server = handle.binding.get("server", {})
    return [
        ToolHandle(
            tool_type="mcp",
            name=item["name"],
            description=item.get("description") or item["name"],
            parameters=item.get("parameters") or {"type": "object"},
            binding={"server": server, "remote": item["remote"]},
            permissions=list(handle.permissions),
        )
        for item in handle.binding.get("toolkit", [])
    ]


@implementation("mcp")
async def _mcp_tool(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    remote = tool.binding.get("remote")
    if not remote:
        raise ToolError("This MCP toolkit must be expanded before use")
    try:
        result = await call_remote_tool(ctx, tool.binding["server"], remote, args)
    except NodeError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    value, text = result_value(result)
    if result.is_error:
        raise ToolError(text or "The MCP tool reported an error")
    return value


# ---- MCP Call -------------------------------------------------------------------------------------
class McpCallConfig(McpConnection):
    tool: str = Field(min_length=1, max_length=200, description="Name of the MCP tool to call.")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="Default arguments; the 'arguments' input is merged on top."
    )
    fail_on_tool_error: bool = Field(
        default=True, description="Fail the node when the tool reports an error (else output it)."
    )


@define_node(
    type="agenticmind.agents.mcp_call",
    version="1.0.0",
    display_name="MCP Call",
    category="tools",
    package=PACKAGE,
    description="Calls one tool on a Model Context Protocol server directly (no model).",
    inputs=[InputPort(name="arguments", type=PortType.JSON, required=False, description="Tool arguments object")],
    outputs=[
        OutputPort(name="result", type=PortType.ANY, description="Structured result, or parsed JSON / text"),
        OutputPort(name="text", type=PortType.STRING),
        OutputPort(name="is_error", type=PortType.BOOLEAN),
    ],
    config=McpCallConfig,
    permissions=Permissions(network=True, subprocess=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=300, max_attempts=1, idempotent=False),
    tags=["tools", "mcp"],
)
async def mcp_call(ctx: NodeContext, inputs, config: McpCallConfig) -> NodeResult:
    extra = inputs.get("arguments")
    if extra is not None and not isinstance(extra, dict):
        raise NodeError("MCP_ARGUMENTS_INVALID", "arguments must be a JSON object")
    binding = McpConnection.model_validate(config.model_dump(include=set(McpConnection.model_fields))).binding()
    result = await call_remote_tool(ctx, binding, config.tool, {**config.arguments, **(extra or {})})
    value, text = result_value(result)
    if result.is_error and config.fail_on_tool_error:
        raise NodeError("MCP_TOOL_ERROR", text or f"MCP tool '{config.tool}' reported an error")
    return NodeResult(outputs={"result": value, "text": text, "is_error": bool(result.is_error)})

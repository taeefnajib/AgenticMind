"""Tool nodes and their implementations.

A tool node outputs a ``ToolHandle`` (name, description, JSON-Schema parameters, binding). The Agent node
looks up the implementation by ``tool_type`` and calls it with validated arguments. Implementations raise
``ToolError`` for problems the model should see and react to (bad input, nothing found, blocked URL).
"""

from __future__ import annotations

import ast
import asyncio
import ipaddress
import math
import operator
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agenticmind_node_sdk import (
    EmbeddingModelHandle,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeResult,
    OutputPort,
    PortType,
    ToolHandle,
    VectorStoreHandle,
    define_node,
    ui,
)
from agenticmind_node_sdk.handles import TOOL_NAME_PATTERN, handle_value
from pydantic import Field, field_validator

PACKAGE = "agenticmind-nodes-agents"


class ToolError(Exception):
    """A tool failure reported back to the model (not a node failure)."""


ToolFn = Callable[[NodeContext, ToolHandle, dict[str, Any]], Awaitable[Any]]
TOOL_IMPLEMENTATIONS: dict[str, ToolFn] = {}


def implementation(tool_type: str) -> Callable[[ToolFn], ToolFn]:
    def decorator(fn: ToolFn) -> ToolFn:
        TOOL_IMPLEMENTATIONS[tool_type] = fn
        return fn

    return decorator


def _name_field(default: str) -> Any:
    return Field(default=default, pattern=TOOL_NAME_PATTERN, description="Name the model uses to call the tool.")


def _description_field(default: str) -> Any:
    return Field(
        default=default,
        min_length=1,
        max_length=2000,
        description="Tells the model when and how to use this tool.",
        json_schema_extra=ui(widget="textarea"),
    )


# ---- Knowledge Search -------------------------------------------------------------------------------
class KnowledgeSearchConfig(NodeConfig):
    name: str = _name_field("search_documents")
    description: str = _description_field(
        "Search the indexed documents for passages relevant to a query. Use it for any question about the "
        "documents' content. Returns numbered passages with their source file, page and section."
    )
    top_k: int = Field(default=5, ge=1, le=20)


@define_node(
    type="agenticmind.agents.tool_knowledge_search",
    version="1.0.0",
    display_name="Knowledge Search Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an agent search your indexed documents (connect the same Embedding Model and Vector Store).",
    inputs=[
        InputPort(name="model", type=PortType.EMBEDDING_MODEL),
        InputPort(name="store", type=PortType.VECTOR_STORE_HANDLE),
    ],
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=KnowledgeSearchConfig,
    tags=["tools", "rag"],
)
async def knowledge_search_tool(ctx: NodeContext, inputs, config: KnowledgeSearchConfig) -> NodeResult:
    handle = ToolHandle(
        tool_type="knowledge_search",
        name=config.name,
        description=config.description,
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        binding={"model": inputs["model"], "store": inputs["store"], "top_k": config.top_k},
        permissions=["network", "filesystem", "credentials", "database"],
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("knowledge_search")
async def _knowledge_search(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    from agenticmind_node_sdk import NodeError
    from agenticmind_nodes_rag.retrieval import embed_texts, open_vector_store, store_call

    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("query must not be empty")
    model = EmbeddingModelHandle.model_validate(tool.binding["model"])
    store_handle = VectorStoreHandle.model_validate(tool.binding["store"])
    try:
        store = open_vector_store(ctx, store_handle)
        if not await store_call(store.exists):
            raise ToolError("The knowledge base is empty. The documents have not been indexed yet.")
        vec = (await embed_texts(ctx, model, [query], "query")).vectors[0]
        hits = await store_call(store.query, vec, int(tool.binding.get("top_k", 5)), None)
    except NodeError as exc:
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    return {
        "results": [
            {
                "n": i,
                "id": h.id,
                "filename": h.document.get("source", {}).get("filename"),
                "page": h.document.get("source", {}).get("page"),
                "section": h.document.get("source", {}).get("section"),
                "score": round(h.score, 4),
                "content": str(h.document.get("content", ""))[:1500],
            }
            for i, h in enumerate(hits, start=1)
        ]
    }


# ---- Calculator ----------------------------------------------------------------------------------
class CalculatorConfig(NodeConfig):
    name: str = _name_field("calculator")
    description: str = _description_field(
        "Evaluate an arithmetic expression exactly, e.g. '25 * 0.6' or 'sqrt(2) + 3**2'. Supports + - * / // % **, "
        "parentheses, sqrt, log, log10, exp, sin, cos, tan, abs, round, min, max, pi and e. Use it for any calculation."
    )


_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_eval(expression: str) -> float | int:
    """Evaluate arithmetic without ``eval``: only literals, arithmetic operators and whitelisted functions."""
    if len(expression) > 300:
        raise ToolError("Expression is too long (max 300 characters)")
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except SyntaxError as exc:
        raise ToolError(f"Invalid expression: {exc.msg}") from exc

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
                raise ToolError("Exponent too large")
            return _BIN_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
            and len(node.args) <= 4
        ):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ToolError(f"Unsupported element in expression: {ast.dump(node)[:60]}")

    try:
        result = ev(tree)
    except ToolError:
        raise
    except (ArithmeticError, ValueError, TypeError) as exc:
        raise ToolError(f"Cannot evaluate: {exc}") from exc
    if isinstance(result, float) and not math.isfinite(result):
        raise ToolError("Result is not a finite number")
    return result


@define_node(
    type="agenticmind.agents.tool_calculator",
    version="1.0.0",
    display_name="Calculator Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an agent do exact arithmetic (safe expression evaluator, no code execution).",
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=CalculatorConfig,
    tags=["tools"],
)
async def calculator_tool(ctx: NodeContext, inputs, config: CalculatorConfig) -> NodeResult:
    handle = ToolHandle(
        tool_type="calculator",
        name=config.name,
        description=config.description,
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "Arithmetic expression"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("calculator")
async def _calculator(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    expression = str(args.get("expression", ""))
    return {"expression": expression, "result": safe_eval(expression)}


# ---- Date & time ---------------------------------------------------------------------------------
class DateTimeConfig(NodeConfig):
    name: str = _name_field("current_datetime")
    description: str = _description_field(
        "Get the current date and time. Optionally pass an IANA timezone such as 'Europe/Oslo' or 'Asia/Dhaka'."
    )


@define_node(
    type="agenticmind.agents.tool_datetime",
    version="1.0.0",
    display_name="Date & Time Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an agent look up the current date and time in any timezone.",
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=DateTimeConfig,
    tags=["tools"],
)
async def datetime_tool(ctx: NodeContext, inputs, config: DateTimeConfig) -> NodeResult:
    handle = ToolHandle(
        tool_type="datetime",
        name=config.name,
        description=config.description,
        parameters={
            "type": "object",
            "properties": {"timezone": {"type": "string", "description": "IANA timezone, default UTC"}},
            "additionalProperties": False,
        },
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("datetime")
async def _datetime(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    tz_name = str(args.get("timezone") or "UTC")
    try:
        tz = UTC if tz_name.upper() == "UTC" else ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError(f"Unknown timezone '{tz_name}'") from exc
    now = datetime.now(tz)
    return {"timezone": tz_name, "iso": now.isoformat(timespec="seconds"), "weekday": now.strftime("%A")}


# ---- Web Fetch ------------------------------------------------------------------------------------
class WebFetchConfig(NodeConfig):
    name: str = _name_field("fetch_web_page")
    description: str = _description_field(
        "Fetch the text of a public web page by URL. Only pages on the allowed domains can be fetched."
    )
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Domains the agent may fetch from, e.g. ['wikipedia.org']. Subdomains are included. "
        "Required: the agent cannot reach any other site.",
    )
    max_chars: int = Field(default=8000, ge=200, le=50_000, json_schema_extra=ui(advanced=True))
    timeout_seconds: float = Field(default=15, gt=0, le=60, json_schema_extra=ui(advanced=True))

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        cleaned = [d.strip().lower().lstrip(".") for d in v if d.strip()]
        if not cleaned:
            raise ValueError("list at least one allowed domain (explicit network egress)")
        for d in cleaned:
            if "/" in d or ":" in d or " " in d:
                raise ValueError(f"'{d}' must be a bare domain such as example.org")
        return cleaned


def _domain_allowed(host: str, allowed: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in allowed)


async def _assert_public_host(host: str, port: int) -> None:
    """Block loopback, private, link-local (cloud metadata) and other non-public addresses."""
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ToolError(f"Cannot resolve host '{host}'") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global or addr.is_multicast:
            raise ToolError(f"'{host}' resolves to a non-public address; blocked")


def _html_to_text(raw: bytes) -> str:
    from agenticmind_nodes_data.parsers import parse_html

    return parse_html(raw)[0].content


@define_node(
    type="agenticmind.agents.tool_web_fetch",
    version="1.0.0",
    display_name="Web Fetch Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an agent read public web pages, restricted to the domains you allow.",
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=WebFetchConfig,
    tags=["tools", "integration"],
)
async def web_fetch_tool(ctx: NodeContext, inputs, config: WebFetchConfig) -> NodeResult:
    handle = ToolHandle(
        tool_type="web_fetch",
        name=config.name,
        description=f"{config.description} Allowed domains: {', '.join(config.allowed_domains)}.",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full http(s) URL"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        binding={
            "allowed_domains": config.allowed_domains,
            "max_chars": config.max_chars,
            "timeout_seconds": config.timeout_seconds,
        },
        permissions=["network"],
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("web_fetch")
async def _web_fetch(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    import httpx

    allowed = list(tool.binding.get("allowed_domains", []))
    url = str(args.get("url", "")).strip()
    timeout = float(tool.binding.get("timeout_seconds", 15))
    for _hop in range(4):  # follow up to 3 redirects, re-checking every hop
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ToolError("Only absolute http(s) URLs are allowed")
        if not _domain_allowed(parsed.hostname, allowed):
            raise ToolError(f"'{parsed.hostname}' is not an allowed domain ({', '.join(allowed)})")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        await _assert_public_host(parsed.hostname, port)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.get(url, headers={"User-Agent": "AgenticMind-WebFetch/0.1"})
        except httpx.HTTPError as exc:
            raise ToolError(f"Request failed: {exc}") from exc
        if resp.is_redirect and resp.headers.get("location"):
            url = str(resp.url.join(resp.headers["location"]))
            continue
        break
    else:
        raise ToolError("Too many redirects")
    if resp.status_code >= 400:
        raise ToolError(f"The page returned HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    raw = resp.content[:2_000_000]
    if "html" in ctype:
        text = await asyncio.to_thread(_html_to_text, raw)
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        text = raw.decode(resp.encoding or "utf-8", errors="replace")
    else:
        raise ToolError(f"Unsupported content type '{ctype}'")
    limit = int(tool.binding.get("max_chars", 8000))
    return {"url": str(resp.url), "truncated": len(text) > limit, "text": text[:limit]}

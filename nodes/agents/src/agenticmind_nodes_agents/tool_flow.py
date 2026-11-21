"""Tool routing and tool chaining (ADR-0029).

* **Tool Router** - one request → one tool. The tool is chosen by keywords, by the model (function calling
  with ``tool_choice="required"`` over all connected tools), or both (keywords first); the model also fills in
  the tool's arguments from the request. Cheaper and more predictable than an Agent loop when a request needs
  exactly one tool.
* **Tool Chain** - a fixed sequence of tool calls. Each step's arguments are templates over the chain input
  and earlier results (``{{input}}``, ``{{previous}}``, ``{{steps.0.result}}``, dotted paths), so the output of
  one tool feeds the next without a model in between.

Both reuse the Agent's tool execution: arguments are checked against the tool's JSON Schema, tools run with
timeouts and permission checks, and every call is a ``tool_call`` trace event.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from agenticmind_node_sdk import (
    Cardinality,
    ExecutionPolicy,
    InputPort,
    ModelUsage,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    SecretRef,
    ToolHandle,
    TransientNodeError,
    define_node,
    secret_field,
    ui,
)
from agenticmind_providers import (
    ChatMessage,
    ChatProviderName,
    ChatRequest,
    ProviderError,
    ToolSpec,
    check_chat_provider,
    get_provider,
)
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent import AGENT_PERMISSIONS, AGENT_PERMISSIONS_V2, _run_tool, check_tools, granting, load_tools
from .tools import PACKAGE

_TOOLS_IN = InputPort(
    name="tools", type=PortType.TOOL, cardinality=Cardinality.MULTIPLE, description="Tools to route between / chain"
)


# ---- Tool Router --------------------------------------------------------------------------------
class ToolRouterConfig(NodeConfig):
    strategy: Literal["model", "keywords", "hybrid"] = Field(
        default="hybrid",
        description="keywords: the first tool whose keywords appear in the request. model: the model picks the "
        "tool from the tools' descriptions. hybrid: keywords first, then the model.",
    )
    keywords: dict[str, list[str]] = Field(
        default_factory=dict,
        description='Tool name → words that select it, e.g. {"calculator": ["sum", "%"], "web_fetch": ["http"]}.',
    )
    default_tool: str | None = Field(default=None, description="Used when no tool is selected (empty = fail).")
    provider: ChatProviderName = Field(default="openrouter")
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1, description="Picks tools / fills arguments.")
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    tool_timeout_seconds: float = Field(default=30, gt=0, le=300, json_schema_extra=ui(advanced=True))
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> ToolRouterConfig:
        check_chat_provider(self.provider, self.api_key, self.base_url)
        return self


def keyword_tool(query: str, keywords: dict[str, list[str]], names: list[str]) -> tuple[str, str] | None:
    lowered = query.lower()
    for name in names:  # in the order the tools are connected
        for word in keywords.get(name, []):
            w = word.strip().lower()
            if w and re.search(rf"(?<!\w){re.escape(w)}(?!\w)" if w[:1].isalnum() else re.escape(w), lowered):
                return name, word.strip()
    return None


async def _pick(
    ctx: NodeContext, config: ToolRouterConfig, query: str, tools: list[ToolHandle]
) -> tuple[str, dict, Any]:
    """One function-calling turn with tool_choice=required → (tool name, arguments, usage)."""
    try:
        resp = await get_provider(config.provider).chat(
            ChatRequest(
                model=config.model,
                system="Call exactly one tool that best serves the request, with arguments taken from the request.",
                messages=[ChatMessage(role="user", content=query)],
                tools=[ToolSpec(name=t.name, description=t.description, parameters=t.parameters) for t in tools],
                tool_choice="required",
                temperature=0,
                max_tokens=1024,
            ),
            api_key=ctx.secret(config.api_key) if config.api_key else None,
            base_url=config.base_url,
        )
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc
    if not resp.tool_calls:
        raise NodeError("TOOL_ROUTE_FAILED", "The model did not choose a tool (does it support tool calling?)")
    call = resp.tool_calls[0]
    return call.name, call.arguments, resp.usage


_TOOL_ROUTER = dict(
    type="agenticmind.agents.tool_router",
    display_name="Tool Router",
    category="agents",
    package=PACKAGE,
    description="Sends a request to the single best tool (by keywords and/or the model), fills its arguments by "
    "function calling and returns the tool's result.",
    inputs=[InputPort(name="query", type=PortType.STRING, description="The request"), _TOOLS_IN],
    outputs=[
        OutputPort(name="result", type=PortType.JSON, description="The tool's result"),
        OutputPort(name="tool", type=PortType.STRING, description="Name of the tool that ran"),
        OutputPort(name="text", type=PortType.STRING, description="The result as text"),
    ],
    config=ToolRouterConfig,
    execution=ExecutionPolicy(timeout_seconds=300, max_attempts=1, idempotent=False),
    tags=["agents", "tools", "routing"],
)


async def _tool_router(ctx: NodeContext, inputs, config: ToolRouterConfig) -> NodeResult:
    tools = load_tools(inputs.get("tools"))
    if not tools:
        raise NodeError("NO_TOOLS", "Connect at least one tool")
    by_name = check_tools(tools)
    query = inputs["query"]
    unknown = [
        n for n in [*config.keywords, *([config.default_tool] if config.default_tool else [])] if n not in by_name
    ]
    if unknown:
        raise NodeError("TOOL_UNKNOWN", f"Configured tool(s) not connected: {', '.join(unknown)}")
    chosen: str | None = None
    how, args, usage = "default", None, None
    if config.strategy in ("keywords", "hybrid"):
        hit = keyword_tool(query, config.keywords, list(by_name))
        if hit:
            chosen, how = hit[0], f"keyword '{hit[1]}'"
    if chosen is None and config.strategy in ("model", "hybrid"):
        chosen, args, usage = await _pick(ctx, config, query, tools)
        how = "model"
    if chosen is None and config.default_tool:
        chosen = config.default_tool
    if chosen is None:
        raise NodeError("TOOL_ROUTE_FAILED", "No tool matched the request and no default tool is set")
    tool = by_name.get(chosen)
    if tool is None:
        raise NodeError("TOOL_UNKNOWN", f"The model chose an unknown tool '{chosen}'")
    if args is None:  # chosen by keyword / default: the model fills in this tool's arguments
        needs_args = bool((tool.parameters or {}).get("properties"))
        args = {}
        if needs_args:
            _, args, usage = await _pick(ctx, config, query, [tool])
    await ctx.emit("tool_routed", {"tool": chosen, "how": how})
    step = await _run_tool(ctx, by_name, chosen, args, config.tool_timeout_seconds)
    result = step.pop("_result")
    if not step["ok"]:
        raise NodeError("TOOL_FAILED", f"{chosen}: {step.get('error')}")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return NodeResult(
        outputs={"result": result, "tool": chosen, "text": text},
        usage=ModelUsage(provider=usage.provider, model=usage.model, input_tokens=usage.input_tokens,
                         output_tokens=usage.output_tokens, estimated_cost_usd=usage.estimated_cost_usd)
        if usage else None,
    )  # fmt: skip


# ---- Tool Chain ---------------------------------------------------------------------------------
class ChainStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, description="Name of a connected tool")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments; strings may use {{input}}, {{previous}}, {{steps.0.result}} or dotted paths.",
    )


class ToolChainConfig(NodeConfig):
    steps: list[ChainStep] = Field(
        default_factory=list,
        min_length=1,
        max_length=20,
        description='Tool calls in order, e.g. [{"tool": "calculator", "arguments": {"expression": "{{input}}"}}, '
        '{"tool": "web_fetch", "arguments": {"url": "https://example.org/{{previous.value}}"}}].',
    )
    stop_on_error: bool = Field(default=True, description="Fail at the first failing step (else continue).")
    tool_timeout_seconds: float = Field(default=30, gt=0, le=300, json_schema_extra=ui(advanced=True))


_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][\w.\-]*)\s*\}\}")


def _lookup(scope: dict[str, Any], path: str) -> Any:
    value: Any = scope
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.lstrip("-").isdigit() and -len(value) <= int(part) < len(value):
            value = value[int(part)]
        else:
            raise KeyError(path)
    return value


def render(value: Any, scope: dict[str, Any]) -> Any:
    """Fill templates in strings (recursively). A string that is exactly one template keeps the value's type."""
    if isinstance(value, dict):
        return {k: render(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scope) for v in value]
    if not isinstance(value, str):
        return value
    whole = _TEMPLATE.fullmatch(value.strip())
    if whole:
        return _lookup(scope, whole.group(1))

    def sub(m: re.Match[str]) -> str:
        found = _lookup(scope, m.group(1))
        return found if isinstance(found, str) else json.dumps(found, ensure_ascii=False, default=str)

    return _TEMPLATE.sub(sub, value)


_TOOL_CHAIN = dict(
    type="agenticmind.agents.tool_chain",
    display_name="Tool Chain",
    category="agents",
    package=PACKAGE,
    description="Runs connected tools in a fixed order; each step's arguments can use the input and earlier "
    "results ({{input}}, {{previous}}, {{steps.0.result}}).",
    inputs=[
        InputPort(name="input", type=PortType.ANY, required=False, description="Available as {{input}}"),
        _TOOLS_IN,
    ],
    outputs=[
        OutputPort(name="result", type=PortType.JSON, description="The last step's result"),
        OutputPort(name="steps", type=PortType.JSON, description="Every step: tool, arguments, ok, result"),
    ],
    config=ToolChainConfig,
    execution=ExecutionPolicy(timeout_seconds=600, max_attempts=1, idempotent=False),
    tags=["agents", "tools"],
)


async def _tool_chain(ctx: NodeContext, inputs, config: ToolChainConfig) -> NodeResult:
    tools = load_tools(inputs.get("tools"))
    by_name = check_tools(tools)
    missing = sorted({s.tool for s in config.steps} - set(by_name))
    if missing:
        raise NodeError("TOOL_UNKNOWN", f"Steps use tools that are not connected: {', '.join(missing)}")
    done: list[dict[str, Any]] = []
    previous: Any = inputs.get("input")
    for i, step_cfg in enumerate(config.steps):
        scope = {"input": inputs.get("input"), "previous": previous, "steps": done}
        try:
            args = render(step_cfg.arguments, scope)
        except KeyError as exc:
            raise NodeError(
                "TEMPLATE_MISSING_VALUE", f"Step {i} ({step_cfg.tool}): no value for {{{{{exc.args[0]}}}}}"
            ) from exc
        step = await _run_tool(ctx, by_name, step_cfg.tool, args, config.tool_timeout_seconds)
        result = step.pop("_result")
        step.pop("_usage", None)
        record = {"tool": step_cfg.tool, "arguments": args, "ok": step["ok"], "result": result,
                  "duration_ms": step.get("duration_ms")}  # fmt: skip
        done.append(record)
        if not step["ok"] and config.stop_on_error:
            raise NodeError("TOOL_CHAIN_FAILED", f"Step {i} ({step_cfg.tool}) failed: {step.get('error')}")
        previous = result
    return NodeResult(outputs={"result": previous, "steps": done})


#: 1.1.0 may run tools that start local processes (stdio MCP servers, ADR-0033).
tool_router = define_node(**_TOOL_ROUTER, version="1.1.0", permissions=AGENT_PERMISSIONS_V2)(
    granting(AGENT_PERMISSIONS_V2)(_tool_router)
)
tool_router_v1 = define_node(**_TOOL_ROUTER, version="1.0.0", permissions=AGENT_PERMISSIONS)(
    granting(AGENT_PERMISSIONS)(_tool_router)
)


#: 1.1.0 may run tools that start local processes (stdio MCP servers, ADR-0033).
tool_chain = define_node(**_TOOL_CHAIN, version="1.1.0", permissions=AGENT_PERMISSIONS_V2)(
    granting(AGENT_PERMISSIONS_V2)(_tool_chain)
)
tool_chain_v1 = define_node(**_TOOL_CHAIN, version="1.0.0", permissions=AGENT_PERMISSIONS)(
    granting(AGENT_PERMISSIONS)(_tool_chain)
)

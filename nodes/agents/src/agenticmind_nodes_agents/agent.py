"""Agent node: a bounded model ↔ tool loop.

Each turn the model sees the task, the conversation so far and the declared tools. Tool calls are
checked (known tool, permissions, JSON-Schema arguments) before execution, run with a timeout, and their
results (or errors) are returned to the model. The loop ends with a final answer or when a limit is hit
(turns, tool calls, tokens, cost). Every model turn and tool call is a trace event.
"""

from __future__ import annotations

import asyncio
import json
import time
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
    Permissions,
    PortType,
    SecretRef,
    ToolHandle,
    TransientNodeError,
    define_node,
    secret_field,
    ui,
)
from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, ToolSpec, get_provider
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from pydantic import Field, model_validator

from .tools import PACKAGE, TOOL_IMPLEMENTATIONS, ToolError

DEFAULT_INSTRUCTIONS = (
    "You are a careful assistant. Use the available tools whenever they help answer accurately, and never "
    "invent tool results. When you use document search results, mention the source file and section. "
    "Answer concisely."
)
MAX_TOOL_RESULT_CHARS = 12_000
AGENT_PERMISSIONS = Permissions(network=True, filesystem=True, credentials=True, database=True)


class AgentConfig(NodeConfig):
    provider: Literal["openrouter", "openai_compatible", "mock"] = Field(
        default="openrouter", description="Model provider (needs a model that supports tool calling)."
    )
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    instructions: str = Field(
        default=DEFAULT_INSTRUCTIONS, description="System instructions.", json_schema_extra=ui(widget="textarea")
    )
    max_turns: int = Field(default=6, ge=1, le=30, description="Maximum model calls.")
    max_tool_calls: int = Field(default=12, ge=0, le=100, description="Maximum tool executions in total.")
    token_budget: int = Field(
        default=60_000, ge=1_000, le=2_000_000, description="Stop when input+output tokens exceed this."
    )
    cost_budget_usd: float = Field(
        default=0.25, ge=0, le=100, description="Stop when the reported cost exceeds this (0 = no limit)."
    )
    on_limit: Literal["fail", "return_partial"] = Field(
        default="fail",
        description="fail: the node fails when a limit is hit. return_partial: return what the agent has so far.",
        json_schema_extra=ui(advanced=True),
    )
    tool_timeout_seconds: float = Field(default=30, gt=0, le=300, json_schema_extra=ui(advanced=True))
    temperature: float = Field(default=0.2, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=1500, ge=16, le=100_000, json_schema_extra=ui(advanced=True))
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> AgentConfig:
        if self.provider == "openrouter" and self.api_key is None:
            raise ValueError('openrouter needs an api_key secret reference (e.g. {"$secret": "OPENROUTER_API_KEY"})')
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible needs a base_url")
        return self


class _Limit(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _validate_arguments(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(schema).iter_errors(args), key=lambda e: list(e.path))
    return [f"{'/'.join(str(p) for p in e.path) or 'arguments'}: {e.message}" for e in errors[:5]]


def _granted(permissions: Permissions) -> set[str]:
    return {name for name, on in permissions.model_dump().items() if on is True}


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


@define_node(
    type="agenticmind.agents.agent",
    version="1.0.0",
    display_name="Agent",
    category="agents",
    package=PACKAGE,
    description="A model that decides which connected tools to call, within turn, tool-call, token and cost limits.",
    inputs=[
        InputPort(name="task", type=PortType.STRING, description="The question or task"),
        InputPort(
            name="tools",
            type=PortType.TOOL,
            required=False,
            cardinality=Cardinality.MULTIPLE,
            description="Tools the agent may use",
        ),
        InputPort(name="context", type=PortType.STRING, required=False, description="Extra background for the agent"),
    ],
    outputs=[
        OutputPort(name="answer", type=PortType.STRING),
        OutputPort(name="result", type=PortType.JSON, description="{answer, steps, report} for chat/API"),
        OutputPort(name="transcript", type=PortType.JSON, description="Full message history (for debugging)"),
    ],
    config=AgentConfig,
    permissions=AGENT_PERMISSIONS,
    # The loop may perform side effects through tools, so the whole node is never retried blindly.
    execution=ExecutionPolicy(timeout_seconds=600, max_attempts=1, idempotent=False),
    tags=["agents"],
)
async def agent(ctx: NodeContext, inputs, config: AgentConfig) -> NodeResult:
    tools = [ToolHandle.model_validate(t) for t in inputs.get("tools", [])]
    names = [t.name for t in tools]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise NodeError("DUPLICATE_TOOL_NAME", f"Tool names must be unique; duplicated: {', '.join(dupes)}")
    granted = _granted(AGENT_PERMISSIONS)
    for t in tools:
        if t.tool_type not in TOOL_IMPLEMENTATIONS:
            raise NodeError("TOOL_UNKNOWN", f"No implementation for tool type '{t.tool_type}' ({t.name})")
        missing = set(t.permissions) - granted
        if missing:
            raise NodeError(
                "TOOL_PERMISSION_DENIED", f"Tool '{t.name}' needs permissions: {', '.join(sorted(missing))}"
            )
    by_name = {t.name: t for t in tools}
    specs = [ToolSpec(name=t.name, description=t.description, parameters=t.parameters) for t in tools]

    system = config.instructions
    if inputs.get("context"):
        system += "\n\nBackground:\n" + inputs["context"]
    messages: list[ChatMessage] = [ChatMessage(role="user", content=inputs["task"])]
    try:
        provider = get_provider(config.provider)
    except ProviderError as exc:
        raise NodeError(exc.code, str(exc)) from exc
    api_key = ctx.secret(config.api_key) if config.api_key else None

    steps: list[dict[str, Any]] = []
    tokens_in = tokens_out = 0
    cost = 0.0
    cost_known = False
    tool_calls_used = 0
    turns = 0
    answer = ""
    stop_reason = "final_answer"

    def check_budgets() -> None:
        if tokens_in + tokens_out >= config.token_budget:
            raise _Limit("token_budget", f"Token budget of {config.token_budget:,} reached")
        if config.cost_budget_usd > 0 and cost_known and cost >= config.cost_budget_usd:
            raise _Limit("cost_budget", f"Cost budget of ${config.cost_budget_usd} reached")

    try:
        while True:
            if turns >= config.max_turns:
                raise _Limit("max_turns", f"No final answer after {config.max_turns} turns")
            check_budgets()
            turns += 1
            try:
                resp = await provider.chat(
                    ChatRequest(
                        model=config.model,
                        system=system,
                        messages=messages,
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        tools=specs,
                        tool_choice="auto" if specs else None,
                    ),
                    api_key=api_key,
                    base_url=config.base_url,
                )
            except ProviderError as exc:
                if exc.retryable:
                    raise TransientNodeError(exc.code, str(exc)) from exc
                raise NodeError(exc.code, str(exc)) from exc
            tokens_in += resp.usage.input_tokens
            tokens_out += resp.usage.output_tokens
            if resp.usage.estimated_cost_usd is not None:
                cost += resp.usage.estimated_cost_usd
                cost_known = True
            await ctx.emit(
                "agent_turn",
                {
                    "turn": turns,
                    "tool_calls": [c.name for c in resp.tool_calls],
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            )

            if not resp.tool_calls:
                answer = resp.text.strip()
                if not answer:
                    raise NodeError(
                        "MODEL_EMPTY_RESPONSE",
                        f"The model returned no answer (finish reason: {resp.finish_reason or 'unknown'})",
                    )
                break

            if tool_calls_used + len(resp.tool_calls) > config.max_tool_calls:
                raise _Limit("max_tool_calls", f"Tool-call limit of {config.max_tool_calls} reached")
            messages.append(ChatMessage(role="assistant", content=resp.text or "", tool_calls=resp.tool_calls))
            for call in resp.tool_calls:
                tool_calls_used += 1
                step = await _run_tool(ctx, by_name, call.name, call.arguments, config.tool_timeout_seconds)
                step["turn"] = turns
                steps.append(step)
                content = json.dumps(step["_result"], ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]
                messages.append(ChatMessage(role="tool", content=content, tool_call_id=call.id, name=call.name))
                del step["_result"]
            check_budgets()
    except _Limit as limit:
        stop_reason = limit.reason
        if config.on_limit == "fail":
            raise NodeError(
                "AGENT_LIMIT_REACHED",
                f"{limit} after {turns} turn(s) and {tool_calls_used} tool call(s). "
                "Raise the limit or set on_limit to return_partial.",
            ) from limit
        answer = next((m.content for m in reversed(messages) if m.role == "assistant" and m.content), "") or (
            f"(stopped: {limit})"
        )

    messages.append(ChatMessage(role="assistant", content=answer))
    report = {
        "stop_reason": stop_reason,
        "turns": turns,
        "tool_calls": tool_calls_used,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(cost, 6) if cost_known else None,
        "model": config.model,
    }
    transcript = [m.model_dump(mode="json", exclude_none=True) for m in messages]
    return NodeResult(
        outputs={
            "answer": answer,
            "result": {"answer": answer, "steps": steps, "report": report},
            "transcript": transcript,
        },
        usage=ModelUsage(
            provider=config.provider,
            model=config.model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            estimated_cost_usd=round(cost, 6) if cost_known else None,
        ),
    )


async def _run_tool(
    ctx: NodeContext, by_name: dict[str, ToolHandle], name: str, arguments: dict[str, Any], timeout: float
) -> dict[str, Any]:
    t0 = time.perf_counter()
    step: dict[str, Any] = {"tool": name, "arguments": arguments, "ok": False}
    tool = by_name.get(name)
    if tool is None:
        result: Any = {"error": f"Unknown tool '{name}'. Available: {', '.join(by_name) or 'none'}"}
    else:
        problems = _validate_arguments(tool.parameters, arguments)
        if problems:
            result = {"error": "Invalid arguments: " + "; ".join(problems)}
        else:
            try:
                result = await asyncio.wait_for(TOOL_IMPLEMENTATIONS[tool.tool_type](ctx, tool, arguments), timeout)
                step["ok"] = True
            except TimeoutError:
                result = {"error": f"Tool timed out after {timeout:g}s"}
            except ToolError as exc:
                result = {"error": str(exc)}
            except NodeError as exc:
                result = {"error": f"{exc.code}: {exc.message}"}
    step["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if step["ok"]:
        step["result_summary"] = _short(result)
    else:
        step["error"] = result["error"]
    step["_result"] = result
    await ctx.emit(
        "tool_call",
        {
            "tool": name,
            "arguments": arguments,
            "ok": step["ok"],
            "duration_ms": step["duration_ms"],
            **({"error": step["error"]} if not step["ok"] else {"result": step["result_summary"]}),
        },
    )
    return step

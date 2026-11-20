"""Agent node: a bounded model ↔ tool loop.

Each turn the model sees the task, the conversation so far and the declared tools. Tool calls are
checked (known tool, permissions, JSON-Schema arguments) before execution, run with a timeout, and their
results (or errors) are returned to the model. The loop ends with a final answer or when a limit is hit
(turns, tool calls, tokens, cost). Every model turn and tool call is a trace event.

``run_agent_loop`` is shared with the Subagent tool (ADR-0014): a supervisor agent delegates to a
subagent through an ordinary, traced tool call, and the subagent's usage counts toward the supervisor's
budgets.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import time
from dataclasses import dataclass
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
from pydantic import Field, model_validator

from .tools import PACKAGE, ToolError, resolve_implementation

DEFAULT_INSTRUCTIONS = (
    "You are a careful assistant. Use the available tools whenever they help answer accurately, and never "
    "invent tool results. When you use document search results, mention the source file and section. "
    "Answer concisely."
)
MAX_TOOL_RESULT_CHARS = 12_000
AGENT_PERMISSIONS = Permissions(
    network=True, filesystem=True, credentials=True, database=True, memory=True, artifacts=True
)
#: Chain of agents currently running (node > subagent > ...), used to label trace events.
CURRENT_AGENT: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar("agenticmind_agent", default=())


class AgentConfig(NodeConfig):
    provider: ChatProviderName = Field(
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
        check_chat_provider(self.provider, self.api_key, self.base_url)
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


MAX_HISTORY_MESSAGES = 200


def _history(raw: Any) -> list[ChatMessage]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_HISTORY_MESSAGES:
        raise NodeError("AGENT_HISTORY_INVALID", f"history must be a list of at most {MAX_HISTORY_MESSAGES} messages")
    out = []
    for i, item in enumerate(raw):
        if (
            not isinstance(item, dict)
            or item.get("role") not in ("user", "assistant")
            or not isinstance(item.get("content"), str)
        ):
            raise NodeError("AGENT_HISTORY_INVALID", f"history[{i}] must be {{role: user|assistant, content: text}}")
        out.append(ChatMessage(role=item["role"], content=item["content"]))
    return out


def check_tools(tools: list[ToolHandle]) -> dict[str, ToolHandle]:
    names = [t.name for t in tools]
    if len(names) != len(set(names)):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise NodeError("DUPLICATE_TOOL_NAME", f"Tool names must be unique; duplicated: {', '.join(dupes)}")
    granted = _granted(AGENT_PERMISSIONS)
    for t in tools:
        if resolve_implementation(t.tool_type) is None:
            raise NodeError("TOOL_UNKNOWN", f"No implementation for tool type '{t.tool_type}' ({t.name})")
        missing = set(t.permissions) - granted
        if missing:
            raise NodeError(
                "TOOL_PERMISSION_DENIED", f"Tool '{t.name}' needs permissions: {', '.join(sorted(missing))}"
            )
    return {t.name: t for t in tools}


@dataclass
class AgentOutcome:
    answer: str
    steps: list[dict[str, Any]]
    report: dict[str, Any]
    transcript: list[dict[str, Any]]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


async def emit_agent(ctx: NodeContext, event_type: str, data: dict[str, Any]) -> None:
    """Emit a trace event labelled with the agent chain when running inside a subagent."""
    path = CURRENT_AGENT.get()
    await ctx.emit(event_type, {**data, **({"agent": " > ".join(path)} if len(path) > 1 else {})})


async def run_agent_loop(
    ctx: NodeContext,
    config: AgentConfig,
    *,
    task: str,
    tools: list[ToolHandle],
    history: list[ChatMessage] | None = None,
    context: str | None = None,
) -> AgentOutcome:
    """The bounded model ↔ tool loop. Raises ``NodeError('AGENT_LIMIT_REACHED')`` when on_limit=fail."""
    by_name = check_tools(tools)
    specs = [ToolSpec(name=t.name, description=t.description, parameters=t.parameters) for t in tools]
    system = config.instructions
    if context:
        system += "\n\nBackground:\n" + context
    messages: list[ChatMessage] = [*(history or []), ChatMessage(role="user", content=task)]
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
            await emit_agent(
                ctx,
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
                    hint = (
                        f" It ran out of output tokens (max_tokens={config.max_tokens}); reasoning models spend "
                        "part of that budget thinking, so raise max_tokens."
                        if resp.finish_reason == "length"
                        else ""
                    )
                    raise NodeError(
                        "MODEL_EMPTY_RESPONSE",
                        f"The model returned no answer (finish reason: {resp.finish_reason or 'unknown'}).{hint}",
                    )
                break

            if tool_calls_used + len(resp.tool_calls) > config.max_tool_calls:
                raise _Limit("max_tool_calls", f"Tool-call limit of {config.max_tool_calls} reached")
            messages.append(ChatMessage(role="assistant", content=resp.text or "", tool_calls=resp.tool_calls))
            for call in resp.tool_calls:
                tool_calls_used += 1
                step = await _run_tool(ctx, by_name, call.name, call.arguments, config.tool_timeout_seconds)
                step["turn"] = turns
                usage = step.pop("_usage", None)
                if usage:  # a subagent's model usage counts toward this agent's budgets
                    tokens_in += int(usage.get("input_tokens", 0))
                    tokens_out += int(usage.get("output_tokens", 0))
                    if usage.get("cost_usd") is not None:
                        cost += float(usage["cost_usd"])
                        cost_known = True
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
        answer = ""
        if limit.reason in ("max_turns", "max_tool_calls"):
            # wrap-up turn: one last call without tools so the work done so far is not lost
            try:
                resp = await provider.chat(
                    ChatRequest(
                        model=config.model,
                        system=system,
                        messages=[
                            *messages,
                            ChatMessage(
                                role="user",
                                content=f"Limit reached ({limit}). No more tool calls are possible. Give your best "
                                "answer now from the information you already have, and say what is missing.",
                            ),
                        ],
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        tools=specs,
                        tool_choice="none" if specs else None,
                    ),
                    api_key=api_key,
                    base_url=config.base_url,
                )
                tokens_in += resp.usage.input_tokens
                tokens_out += resp.usage.output_tokens
                if resp.usage.estimated_cost_usd is not None:
                    cost += resp.usage.estimated_cost_usd
                    cost_known = True
                answer = resp.text.strip()
                await emit_agent(ctx, "agent_wrap_up", {"reason": limit.reason, "answered": bool(answer)})
            except ProviderError:
                answer = ""
        answer = (
            answer
            or next((m.content for m in reversed(messages) if m.role == "assistant" and m.content), "")
            or f"(stopped: {limit})"
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
    return AgentOutcome(
        answer=answer,
        steps=steps,
        report=report,
        transcript=[m.model_dump(mode="json", exclude_none=True) for m in messages],
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cost_usd=round(cost, 6) if cost_known else None,
    )


async def _run_agent(ctx: NodeContext, inputs, config: AgentConfig) -> NodeResult:
    tools = [ToolHandle.model_validate(t) for t in inputs.get("tools", [])]
    token = CURRENT_AGENT.set((ctx.node_id,))
    try:
        out = await run_agent_loop(
            ctx,
            config,
            task=inputs["task"],
            tools=tools,
            history=_history(inputs.get("history")),
            context=inputs.get("context"),
        )
    finally:
        CURRENT_AGENT.reset(token)
    return NodeResult(
        outputs={
            "answer": out.answer,
            "result": {"answer": out.answer, "steps": out.steps, "report": out.report},
            "transcript": out.transcript,
        },
        usage=ModelUsage(
            provider=config.provider,
            model=config.model,
            input_tokens=out.input_tokens,
            output_tokens=out.output_tokens,
            estimated_cost_usd=out.cost_usd,
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
            # a tool may declare its own timeout (a subagent needs longer than a calculator)
            limit = float(tool.binding.get("tool_timeout_seconds", timeout))
            impl = resolve_implementation(tool.tool_type)
            try:
                if impl is None:
                    raise ToolError(f"No implementation for tool type '{tool.tool_type}'")
                result = await asyncio.wait_for(impl(ctx, tool, arguments), limit)
                step["ok"] = True
            except TimeoutError:
                result = {"error": f"Tool timed out after {limit:g}s"}
            except ToolError as exc:
                result = {"error": str(exc)}
            except NodeError as exc:
                result = {"error": f"{exc.code}: {exc.message}"}
    if isinstance(result, dict):  # private fields from a subagent: nested steps, report and usage
        if "_steps" in result:
            step["substeps"] = result.pop("_steps")
        if "_report" in result:
            step["subreport"] = result.pop("_report")
        if "_usage" in result:
            step["_usage"] = result.pop("_usage")
    step["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    if step["ok"]:
        step["result_summary"] = _short(result)
    else:
        step["error"] = result["error"]
    step["_result"] = result
    await emit_agent(
        ctx,
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


_INPUTS_V1 = [
    InputPort(name="task", type=PortType.STRING, description="The question or task"),
    InputPort(
        name="tools",
        type=PortType.TOOL,
        required=False,
        cardinality=Cardinality.MULTIPLE,
        description="Tools the agent may use (including Subagent tools to delegate to)",
    ),
    InputPort(name="context", type=PortType.STRING, required=False, description="Extra background for the agent"),
]
_HISTORY = InputPort(
    name="history",
    type=PortType.JSON,
    required=False,
    description="Earlier messages [{role, content}], e.g. from Conversation History",
)
_OUTPUTS = [
    OutputPort(name="answer", type=PortType.STRING),
    OutputPort(name="result", type=PortType.JSON, description="{answer, steps, report} for chat/API"),
    OutputPort(name="transcript", type=PortType.JSON, description="Full message history (for debugging)"),
]
_COMMON: dict[str, Any] = {
    "type": "agenticmind.agents.agent",
    "display_name": "Agent",
    "category": "agents",
    "package": PACKAGE,
    "description": "A model that decides which connected tools to call, within turn, tool-call, token and cost limits.",
    "outputs": _OUTPUTS,
    "config": AgentConfig,
    "permissions": AGENT_PERMISSIONS,
    # The loop may perform side effects through tools, so the whole node is never retried blindly.
    "execution": ExecutionPolicy(timeout_seconds=600, max_attempts=1, idempotent=False),
}
#: 1.1.0 adds the optional ``history`` input (conversation memory).
agent = define_node(**_COMMON, version="1.1.0", inputs=[*_INPUTS_V1, _HISTORY], tags=["agents"])(_run_agent)
#: Kept so projects saved with 1.0.0 keep validating and running unchanged.
agent_v1 = define_node(**_COMMON, version="1.0.0", inputs=_INPUTS_V1, tags=["agents", "legacy"])(_run_agent)

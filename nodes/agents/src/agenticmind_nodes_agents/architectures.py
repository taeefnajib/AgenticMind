"""Agent architectures beyond native function calling (ADR-0029).

* **ReAct** (Yao et al., 2022): the model writes ``Thought:`` / ``Action:`` / ``Action Input:`` lines, the
  runtime executes the action and answers with ``Observation:``, until ``Final Answer:``. Tools are described
  in the prompt, so it works with models that have no native tool calling, and every thought is in the trace.
* **Plan-and-execute**: one call writes a short numbered plan; each step then runs as a function-calling
  agent loop that sees the results of earlier steps; a final call combines them into the answer.

Both share the Agent's guarantees: tool arguments are checked against their JSON Schema, tools run with
timeouts, and turn / tool-call / token / cost limits apply to the whole run (``on_limit`` decides between
failing and returning a partial answer).
"""

from __future__ import annotations

import json
import re
from typing import Any

from agenticmind_node_sdk import NodeContext, NodeError, ToolHandle, TransientNodeError
from agenticmind_nodes_core.models import effort
from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, chat_stream, get_provider

from .agent import (
    MAX_TOOL_RESULT_CHARS,
    AgentConfig,
    AgentOutcome,
    _Limit,
    _run_tool,
    check_tools,
    emit_agent,
    run_agent_loop,
)

REACT_FORMAT = """You solve the task by reasoning in steps and using tools. Reply in exactly this format:

Thought: what you know so far and what to do next
Action: one tool name from the list
Action Input: the tool arguments as a JSON object

The runtime then replies with "Observation: ..." (the tool result). Repeat as needed. When you can answer:

Thought: why you can answer now
Final Answer: the answer for the user

Write only one Action per reply. Never write an Observation yourself.

Tools:
{tools}"""

_FINAL = re.compile(r"Final Answer\s*:\s*(.*)\Z", re.S | re.I)
_ACTION = re.compile(r"Action\s*:\s*(.+?)\s*$", re.M | re.I)
_INPUT = re.compile(r"Action Input\s*:\s*(.*)\Z", re.S | re.I)
_THOUGHT = re.compile(r"Thought\s*:\s*(.*?)(?=\n\s*(?:Action|Final Answer)\s*:|\Z)", re.S | re.I)


def parse_react(text: str) -> dict[str, Any]:
    """{"thought", "final"} or {"thought", "action", "input"}; raises ValueError on malformed replies."""
    thought_m = _THOUGHT.search(text)
    thought = thought_m.group(1).strip() if thought_m else ""
    final = _FINAL.search(text)
    action = _ACTION.search(text)
    if final and (not action or final.start() < action.start()):
        return {"thought": thought, "final": final.group(1).strip()}
    if not action:
        raise ValueError("no 'Action:' or 'Final Answer:' line")
    raw = _INPUT.search(text)
    payload = (raw.group(1).strip() if raw else "{}") or "{}"
    payload = payload.split("\nObservation", 1)[0].strip()  # some models continue past their action
    payload = re.sub(r"^```(?:json)?\s*|\s*```$", "", payload).strip()
    try:
        args = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Action Input is not valid JSON ({exc.msg})") from exc
    if not isinstance(args, dict):
        raise ValueError("Action Input must be a JSON object")
    return {"thought": thought, "action": action.group(1).strip().strip("`"), "input": args}


def _tool_list(tools: list[ToolHandle]) -> str:
    return "\n".join(f"- {t.name}: {t.description} Arguments (JSON Schema): {json.dumps(t.parameters)}" for t in tools)


class _Meter:
    """Token / cost / turn accounting shared by the steps of one run."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.tokens_in = self.tokens_out = self.turns = self.tool_calls = 0
        self.cost = 0.0
        self.cost_known = False

    def add(self, usage: Any) -> None:
        self.tokens_in += usage.input_tokens
        self.tokens_out += usage.output_tokens
        if usage.estimated_cost_usd is not None:
            self.cost += usage.estimated_cost_usd
            self.cost_known = True

    def add_outcome(self, out: AgentOutcome) -> None:
        self.tokens_in += out.input_tokens
        self.tokens_out += out.output_tokens
        self.turns += int(out.report.get("turns", 0))
        self.tool_calls += int(out.report.get("tool_calls", 0))
        if out.cost_usd is not None:
            self.cost += out.cost_usd
            self.cost_known = True

    def check(self) -> None:
        c = self.config
        if self.tokens_in + self.tokens_out >= c.token_budget:
            raise _Limit("token_budget", f"Token budget of {c.token_budget:,} reached")
        if c.cost_budget_usd > 0 and self.cost_known and self.cost >= c.cost_budget_usd:
            raise _Limit("cost_budget", f"Cost budget of ${c.cost_budget_usd} reached")

    def report(self, stop_reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "stop_reason": stop_reason, "turns": self.turns, "tool_calls": self.tool_calls,
            "input_tokens": self.tokens_in, "output_tokens": self.tokens_out,
            "cost_usd": round(self.cost, 6) if self.cost_known else None, "model": self.config.model, **extra,
        }  # fmt: skip

    def outcome(self, answer: str, steps: list, report: dict, transcript: list) -> AgentOutcome:
        return AgentOutcome(
            answer=answer, steps=steps, report=report, transcript=transcript, input_tokens=self.tokens_in,
            output_tokens=self.tokens_out, cost_usd=round(self.cost, 6) if self.cost_known else None,
        )  # fmt: skip


async def _chat(ctx: NodeContext, config: AgentConfig, meter: _Meter, *, system: str, messages: list[ChatMessage],
                max_tokens: int | None = None) -> str:  # fmt: skip
    try:
        provider = get_provider(config.provider)
        resp = await chat_stream(  # retries transient failures (ADR-0041)
            provider,
            ChatRequest(
                model=config.model,
                system=system,
                messages=messages,
                temperature=config.temperature,
                max_tokens=max_tokens or config.max_tokens,
                reasoning_effort=effort(config.reasoning_effort),
            ),  # fmt: skip
            api_key=ctx.secret(config.api_key) if config.api_key else None,
            base_url=config.base_url,
        )
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc
    meter.add(resp.usage)
    return resp.text or ""


def _limit_error(limit: _Limit, meter: _Meter) -> NodeError:
    return NodeError(
        "AGENT_LIMIT_REACHED",
        f"{limit} after {meter.turns} turn(s) and {meter.tool_calls} tool call(s). "
        "Raise the limit or set on_limit to return_partial.",
    )


# ---- ReAct --------------------------------------------------------------------------------------
async def run_react_loop(
    ctx: NodeContext,
    config: AgentConfig,
    *,
    task: str,
    tools: list[ToolHandle],
    history: list[ChatMessage] | None = None,
    context: str | None = None,
) -> AgentOutcome:
    by_name = check_tools(tools)
    system = config.instructions + "\n\n" + REACT_FORMAT.format(tools=_tool_list(tools) or "(none: answer directly)")
    if context:
        system += "\n\nBackground:\n" + context
    messages: list[ChatMessage] = [*(history or []), ChatMessage(role="user", content=task)]
    meter = _Meter(config)
    steps: list[dict[str, Any]] = []
    answer, stop_reason = "", "final_answer"
    try:
        while True:
            if meter.turns >= config.max_turns:
                raise _Limit("max_turns", f"No final answer after {config.max_turns} turns")
            meter.check()
            meter.turns += 1
            text = await _chat(ctx, config, meter, system=system, messages=messages)
            messages.append(ChatMessage(role="assistant", content=text))
            try:
                parsed = parse_react(text)
            except ValueError as exc:
                await emit_agent(ctx, "agent_turn", {"turn": meter.turns, "format_error": str(exc)})
                retry = (
                    f"Observation: your reply was not in the format ({exc}). "
                    "Reply with Thought/Action/Action Input or Final Answer."
                )
                messages.append(ChatMessage(role="user", content=retry))
                continue
            await emit_agent(ctx, "agent_thought", {"turn": meter.turns, "thought": parsed["thought"][:500]})
            if "final" in parsed:
                answer = parsed["final"]
                break
            if meter.tool_calls + 1 > config.max_tool_calls:
                raise _Limit("max_tool_calls", f"Tool-call limit of {config.max_tool_calls} reached")
            meter.tool_calls += 1
            step = await _run_tool(ctx, by_name, parsed["action"], parsed["input"], config.tool_timeout_seconds)
            step.update(turn=meter.turns, thought=parsed["thought"])
            step.pop("_usage", None)
            observation = json.dumps(step.pop("_result"), ensure_ascii=False, default=str)[:MAX_TOOL_RESULT_CHARS]
            steps.append(step)
            messages.append(ChatMessage(role="user", content=f"Observation: {observation}"))
    except _Limit as limit:
        stop_reason = limit.reason
        if config.on_limit == "fail":
            raise _limit_error(limit, meter) from limit
        answer = f"(stopped: {limit})"
    return meter.outcome(answer, steps, meter.report(stop_reason, architecture="react"),
                         [m.model_dump(mode="json", exclude_none=True) for m in messages])  # fmt: skip


# ---- Plan-and-execute ---------------------------------------------------------------------------
PLAN_PROMPT = (
    "Break the task into at most {n} short, concrete steps that together answer it (fewer is better; one step is "
    'fine for simple tasks). Reply with JSON only: {{"steps": ["step 1", "step 2"]}}.\n\nAvailable tools:\n{tools}'
)


def parse_plan(text: str, limit: int) -> list[str]:
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            steps = json.loads(match.group(0)).get("steps")
            if isinstance(steps, list) and all(isinstance(s, str) for s in steps) and steps:
                return [s.strip() for s in steps if s.strip()][:limit]
        except (json.JSONDecodeError, AttributeError):
            pass
    lines = [re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", line).strip() for line in text.splitlines()]
    steps = [line for line in lines if line and not line.startswith(("{", "}"))]
    return steps[:limit] or ["Answer the task."]


async def run_plan_and_execute(
    ctx: NodeContext,
    config: AgentConfig,
    *,
    task: str,
    tools: list[ToolHandle],
    history: list[ChatMessage] | None = None,
    context: str | None = None,
) -> AgentOutcome:
    check_tools(tools)
    meter = _Meter(config)
    background = f"\n\nBackground:\n{context}" if context else ""
    tool_list = "\n".join(f"- {t.name}: {t.description}" for t in tools) or "(none)"
    stop_reason = "final_answer"
    steps: list[dict[str, Any]] = []
    plan: list[str] = []
    results: list[str] = []
    try:
        meter.turns += 1
        text = await _chat(ctx, config, meter, system=config.instructions + background,
                           messages=[*(history or []), ChatMessage(role="user", content=PLAN_PROMPT.format(
                               n=config.max_plan_steps, tools=tool_list) + f"\n\nTask: {task}")],
                           max_tokens=max(config.max_tokens, 800))  # fmt: skip
        plan = parse_plan(text, config.max_plan_steps)
        await emit_agent(ctx, "agent_plan", {"steps": plan})
        for i, item in enumerate(plan, start=1):
            meter.check()
            done = "\n".join(f"{j}. {plan[j - 1]} → {r}" for j, r in enumerate(results, start=1)) or "(nothing yet)"
            remaining = max(1, config.max_turns - meter.turns)
            sub = replace_config(config, meter, remaining)
            out = await run_agent_loop(
                ctx, sub,
                task=f"Overall task: {task}\n\nPlan step {i} of {len(plan)}: {item}\n\nResults of earlier steps:\n"
                f"{done}\n\nDo only this step and report its result briefly.",
                tools=tools, context=context,
            )  # fmt: skip
            meter.add_outcome(out)
            results.append(out.answer)
            steps.append({"plan_step": i, "step": item, "result": out.answer, "tools": out.steps,
                          "ok": out.report.get("stop_reason") == "final_answer"})  # fmt: skip
            await emit_agent(ctx, "agent_step_done", {"step": i, "of": len(plan), "result": out.answer[:300]})
        meter.check()
        meter.turns += 1
        findings = "\n".join(f"{i}. {s} → {r}" for i, (s, r) in enumerate(zip(plan, results, strict=True), start=1))
        answer = (await _chat(ctx, config, meter, system=config.instructions + background, messages=[
            ChatMessage(role="user", content=f"Task: {task}\n\nWhat each step found:\n{findings}\n\nWrite the final "
                        "answer for the user, using only these findings.")])).strip()  # fmt: skip
    except _Limit as limit:
        stop_reason = limit.reason
        if config.on_limit == "fail":
            raise _limit_error(limit, meter) from limit
        answer = "\n".join(results) or f"(stopped: {limit})"
    except NodeError as exc:
        if exc.code != "AGENT_LIMIT_REACHED" or config.on_limit == "fail":
            raise
        stop_reason, answer = "step_limit", "\n".join(results) or str(exc)
    return meter.outcome(answer, steps, meter.report(stop_reason, architecture="plan_and_execute", plan=plan), [])


def replace_config(config: AgentConfig, meter: _Meter, turns: int) -> AgentConfig:
    """A step's agent loop gets what is left of the run's budgets."""
    left_tokens = max(1_000, config.token_budget - meter.tokens_in - meter.tokens_out)
    left_calls = max(0, config.max_tool_calls - meter.tool_calls)
    left_cost = max(0.0, config.cost_budget_usd - meter.cost) if config.cost_budget_usd > 0 else 0.0
    return config.model_copy(update={
        "architecture": "function_calling", "max_turns": max(1, min(config.max_turns, turns)),
        "max_tool_calls": left_calls, "token_budget": left_tokens,
        "cost_budget_usd": left_cost if config.cost_budget_usd > 0 else 0, "on_limit": config.on_limit,
    })  # fmt: skip


__all__ = ["parse_plan", "parse_react", "run_plan_and_execute", "run_react_loop"]

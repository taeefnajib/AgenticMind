"""Multi-agent building blocks (ADR-0014).

* **Subagent** (tool) - wraps an agent with its own instructions, model, tools and limits as a tool. A
  supervisor Agent delegates by calling it: the handoff (task + context in, answer out) is an explicit,
  bounded, traced tool call, and the subagent's usage counts toward the supervisor's budgets.
* **Planner** - turns a goal into a short list of self-contained tasks (feed a Loop of workers).
* **Critic** - reviews content against explicit criteria. Deterministic rule checks are reported
  separately from model judgments: a model agreeing is not proof of correctness.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from agenticmind_node_sdk import (
    Cardinality,
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
from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, ToolSpec, get_provider
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent import CURRENT_AGENT, AgentConfig, emit_agent, run_agent_loop
from .tools import PACKAGE, ToolError, _description_field, _name_field, implementation

#: Supervisor > subagent > sub-subagent ... at most this many agents deep.
MAX_AGENT_DEPTH = 3
LLM_PERMISSIONS = Permissions(network=True, credentials=True)


class _ModelConfig(NodeConfig):
    provider: Literal["openrouter", "openai_compatible", "mock"] = Field(default="openrouter")
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))
    temperature: float = Field(default=0.2, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=1500, ge=16, le=100_000, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check_provider(self) -> _ModelConfig:
        if self.provider == "openrouter" and self.api_key is None:
            raise ValueError('openrouter needs an api_key secret reference (e.g. {"$secret": "OPENROUTER_API_KEY"})')
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible needs a base_url")
        return self


async def _forced_call(
    ctx: NodeContext, config: _ModelConfig, *, system: str, user: str, tool: ToolSpec
) -> tuple[dict[str, Any] | None, Any]:
    """Ask the model to answer by calling ``tool``; returns (arguments or None, usage)."""
    request = ChatRequest(
        model=config.model,
        system=system,
        messages=[ChatMessage(role="user", content=user)],
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        tools=[tool],
        tool_choice="required",
    )
    try:
        api_key = ctx.secret(config.api_key) if config.api_key else None
        resp = await get_provider(config.provider).chat(request, api_key=api_key, base_url=config.base_url)
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc
    call = next((c for c in resp.tool_calls if c.name == tool.name), None)
    return (call.arguments if call else None), resp.usage


# ---- Subagent -------------------------------------------------------------------------------------
class SubagentConfig(AgentConfig):
    name: str = _name_field("researcher")
    description: str = _description_field(
        "A research specialist. Give it one clear task (and any context it needs); it returns its findings."
    )
    max_turns: int = Field(default=4, ge=1, le=30, description="Maximum model calls for one delegated task.")
    max_tool_calls: int = Field(default=6, ge=0, le=100)
    token_budget: int = Field(default=30_000, ge=1_000, le=2_000_000)
    cost_budget_usd: float = Field(default=0.10, ge=0, le=100)
    timeout_seconds: float = Field(default=120, gt=0, le=600, description="Time limit for one delegated task.")


@define_node(
    type="agenticmind.agents.subagent",
    version="1.0.0",
    display_name="Subagent",
    category="tools",
    package=PACKAGE,
    description="An agent offered as a tool: a supervisor Agent delegates tasks to it (explicit, bounded handoff).",
    inputs=[
        InputPort(
            name="tools",
            type=PortType.TOOL,
            required=False,
            cardinality=Cardinality.MULTIPLE,
            description="The subagent's own tools",
        )
    ],
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=SubagentConfig,
    # Declares credentials because its config references the model key; the key is resolved only by the
    # agent that runs it (secret references are delegated along tool edges, never values).
    permissions=Permissions(credentials=True),
    tags=["agents", "tools", "multi-agent"],
)
async def subagent(ctx: NodeContext, inputs, config: SubagentConfig) -> NodeResult:
    tools = [ToolHandle.model_validate(t) for t in inputs.get("tools", [])]
    agent_cfg = config.model_dump(mode="json", exclude={"name", "description", "timeout_seconds"}, by_alias=True)
    permissions = sorted({"network", "credentials"} | {p for t in tools for p in t.permissions})
    handle = ToolHandle(
        tool_type="subagent",
        name=config.name,
        description=config.description,
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "One self-contained task for this agent"},
                "context": {"type": "string", "description": "Facts or earlier results it needs (optional)"},
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        binding={
            "agent": agent_cfg,
            "tools": [handle_value(t) for t in tools],
            "tool_timeout_seconds": config.timeout_seconds,
        },
        permissions=permissions,
    )
    return NodeResult(outputs={"tool": handle_value(handle)})


@implementation("subagent")
async def _run_subagent(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    path = CURRENT_AGENT.get()
    if tool.name in path:
        raise ToolError(f"'{tool.name}' is already working on this task (delegation cycle); answer yourself")
    if len(path) >= MAX_AGENT_DEPTH:
        raise ToolError(f"Delegation depth limit ({MAX_AGENT_DEPTH} agents) reached; answer yourself")
    config = AgentConfig.model_validate(tool.binding["agent"])
    tools = [ToolHandle.model_validate(t) for t in tool.binding.get("tools", [])]
    await emit_agent(ctx, "handoff", {"to": tool.name, "task": str(args["task"])[:1000]})
    token = CURRENT_AGENT.set((*path, tool.name))
    try:
        out = await run_agent_loop(ctx, config, task=args["task"], tools=tools, context=args.get("context"))
    except NodeError as exc:
        raise ToolError(f"{tool.name} could not finish: {exc.code}: {exc.message}") from exc
    finally:
        CURRENT_AGENT.reset(token)
    await emit_agent(
        ctx,
        "handoff_result",
        {"from": tool.name, "stop_reason": out.report["stop_reason"], "answer": out.answer[:300]},
    )
    return {
        "answer": out.answer,
        "stop_reason": out.report["stop_reason"],
        "_steps": out.steps,
        "_report": out.report,
        "_usage": {"input_tokens": out.input_tokens, "output_tokens": out.output_tokens, "cost_usd": out.cost_usd},
    }


# ---- Planner --------------------------------------------------------------------------------------
PLANNER_INSTRUCTIONS = (
    "You are a planner. Break the goal into the smallest set of independent tasks that together achieve it. "
    "Each task must be self-contained: a worker sees only that task (and the goal), not the other tasks. "
    "Do not include a final 'combine the results' task; that happens separately."
)


class PlannerConfig(_ModelConfig):
    instructions: str = Field(default=PLANNER_INSTRUCTIONS, json_schema_extra=ui(widget="textarea"))
    max_tasks: int = Field(default=4, ge=1, le=20, description="Upper bound on the number of tasks.")


@define_node(
    type="agenticmind.agents.planner",
    version="1.0.0",
    display_name="Planner",
    category="agents",
    package=PACKAGE,
    description="Breaks a goal into a short list of self-contained tasks (e.g. for a Loop of worker agents).",
    inputs=[
        InputPort(name="goal", type=PortType.STRING),
        InputPort(name="context", type=PortType.STRING, required=False),
    ],
    outputs=[
        OutputPort(name="tasks", type=PortType.JSON, description="List of task texts"),
        OutputPort(name="plan", type=PortType.JSON, description="{goal, tasks: [{title, instructions}], rationale}"),
        OutputPort(name="count", type=PortType.NUMBER),
    ],
    config=PlannerConfig,
    permissions=LLM_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["agents", "multi-agent"],
)
async def planner(ctx: NodeContext, inputs, config: PlannerConfig) -> NodeResult:
    goal = inputs["goal"].strip()
    if not goal:
        raise NodeError("PLAN_EMPTY_GOAL", "The goal is empty")
    tool = ToolSpec(
        name="submit_plan",
        description="Submit the plan",
        parameters={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "maxItems": config.max_tasks,
                    "items": {
                        "type": "object",
                        "properties": {"title": {"type": "string"}, "instructions": {"type": "string"}},
                        "required": ["title", "instructions"],
                    },
                },
                "rationale": {"type": "string"},
            },
            "required": ["tasks"],
        },
    )
    user = f"Goal: {goal}" + (f"\n\nContext:\n{inputs['context']}" if inputs.get("context") else "")
    user += f"\n\nUse at most {config.max_tasks} tasks."
    args, usage = await _forced_call(ctx, config, system=config.instructions, user=user, tool=tool)
    raw = (args or {}).get("tasks")
    if not isinstance(raw, list):
        raise NodeError("PLAN_INVALID", "The model did not return a task list")
    tasks = [
        {"title": str(t.get("title", "")).strip(), "instructions": str(t.get("instructions", "")).strip()}
        for t in raw
        if isinstance(t, dict) and (t.get("title") or t.get("instructions"))
    ]
    if not tasks:
        raise NodeError("PLAN_EMPTY", "The plan has no tasks")
    truncated = len(tasks) > config.max_tasks
    tasks = tasks[: config.max_tasks]
    texts = [f"{t['title']}: {t['instructions']}" if t["title"] else t["instructions"] for t in tasks]
    plan = {"goal": goal, "tasks": tasks, "rationale": str((args or {}).get("rationale", "")), "truncated": truncated}
    await ctx.emit("plan", {"tasks": [t["title"] for t in tasks], "truncated": truncated})
    return NodeResult(outputs={"tasks": texts, "plan": plan, "count": len(texts)}, usage=usage)


# ---- Critic ---------------------------------------------------------------------------------------
class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=500)
    required: bool = True


DEFAULT_CRITERIA = [
    Criterion(name="answers_task", description="Fully does what the task asks."),
    Criterion(name="accurate", description="Contains no invented facts, numbers or promises."),
    Criterion(name="clear", description="Easy to read; no filler.", required=False),
]


class CriticConfig(_ModelConfig):
    criteria: list[Criterion] = Field(
        default=DEFAULT_CRITERIA,
        description="Judged by the model. Leave empty for rule checks only (no model call).",
    )
    min_words: int = Field(default=0, ge=0, le=100_000, description="Rule check (0 = off).")
    max_words: int = Field(default=0, ge=0, le=100_000, description="Rule check (0 = off).")
    must_include: list[str] = Field(default_factory=list, description="Rule check: each phrase must appear.")
    must_not_include: list[str] = Field(default_factory=list, description="Rule check: none may appear.")
    temperature: float = Field(default=0.0, ge=0, le=2, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check_names(self) -> CriticConfig:
        names = [c.name for c in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("criterion names must be unique")
        if self.max_words and self.min_words > self.max_words:
            raise ValueError("min_words is larger than max_words")
        return self


def rule_checks(content: str, config: CriticConfig) -> list[dict[str, Any]]:
    """Deterministic checks: verifiable facts about the text, independent of any model."""
    words = len(re.findall(r"\S+", content))
    lowered = content.lower()
    out: list[dict[str, Any]] = []
    if config.min_words:
        out.append({"check": "min_words", "passed": words >= config.min_words, "detail": f"{words} words"})
    if config.max_words:
        out.append({"check": "max_words", "passed": words <= config.max_words, "detail": f"{words} words"})
    for phrase in config.must_include:
        out.append({"check": "must_include", "passed": phrase.lower() in lowered, "detail": phrase})
    for phrase in config.must_not_include:
        out.append({"check": "must_not_include", "passed": phrase.lower() not in lowered, "detail": phrase})
    return out


@define_node(
    type="agenticmind.agents.critic",
    version="1.0.0",
    display_name="Critic",
    category="agents",
    package=PACKAGE,
    description="Reviews content against explicit criteria and rule checks; routes pass / fail and gives feedback.",
    inputs=[
        InputPort(name="content", type=PortType.STRING, description="What to review"),
        InputPort(name="task", type=PortType.STRING, required=False, description="What the content should achieve"),
    ],
    outputs=[
        OutputPort(name="passed", type=PortType.BOOLEAN),
        OutputPort(name="needs_revision", type=PortType.BOOLEAN, description="not passed (for Loop End continue)"),
        OutputPort(name="feedback", type=PortType.STRING),
        OutputPort(
            name="revision_request",
            type=PortType.STRING,
            description="Task + draft + feedback as a prompt for the next draft (for Loop End next_state)",
        ),
        OutputPort(name="report", type=PortType.JSON),
    ],
    branches=["pass", "fail"],
    config=CriticConfig,
    permissions=LLM_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["agents", "evaluation", "multi-agent"],
)
async def critic(ctx: NodeContext, inputs, config: CriticConfig) -> NodeResult:
    content = inputs["content"]
    task = inputs.get("task") or ""
    rules = rule_checks(content, config)
    judged: list[dict[str, Any]] = []
    feedback_parts: list[str] = []
    usage = None
    if config.criteria:
        names = [c.name for c in config.criteria]
        tool = ToolSpec(
            name="submit_review",
            description="Submit your review",
            parameters={
                "type": "object",
                "properties": {
                    "criteria": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "enum": names},
                                "passed": {"type": "boolean"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "passed", "reason"],
                        },
                    },
                    "feedback": {"type": "string", "description": "Concrete changes that would fix the problems"},
                },
                "required": ["criteria", "feedback"],
            },
        )
        listing = "\n".join(f"- {c.name}: {c.description}" for c in config.criteria)
        system = (
            "You are a strict reviewer. Judge the content against each criterion independently, citing the "
            "text. A criterion passes only if it is clearly met. Call submit_review exactly once."
        )
        user = (f"Task:\n{task}\n\n" if task else "") + f"Criteria:\n{listing}\n\nContent:\n{content}"
        args, usage = await _forced_call(ctx, config, system=system, user=user, tool=tool)
        verdicts = {v.get("name"): v for v in ((args or {}).get("criteria") or []) if isinstance(v, dict)}
        for c in config.criteria:
            v = verdicts.get(c.name)
            judged.append(
                {
                    "name": c.name,
                    "required": c.required,
                    "passed": bool(v and v.get("passed") is True),
                    "reason": str(v.get("reason", "")) if v else "not assessed by the model",
                }
            )
        if (args or {}).get("feedback"):
            feedback_parts.append(str(args["feedback"]).strip())
    failed_rules = [r for r in rules if not r["passed"]]
    for r in failed_rules:
        feedback_parts.append(
            {
                "min_words": f"Too short ({r['detail']}); write at least {config.min_words} words.",
                "max_words": f"Too long ({r['detail']}); keep it within {config.max_words} words.",
                "must_include": f"Must mention: {r['detail']}.",
                "must_not_include": f"Must not contain: {r['detail']}.",
            }[r["check"]]
        )
    passed = not failed_rules and all(j["passed"] for j in judged if j["required"])
    feedback = "\n".join(p for p in feedback_parts if p) or ("Looks good." if passed else "Please improve the draft.")
    revision = (
        (f"Task:\n{task}\n\n" if task else "") + f"Previous draft:\n{content}\n\nReviewer feedback:\n{feedback}\n\n"
        "Rewrite the draft so it fully addresses the feedback. Output only the revised text."
    )
    report = {
        "passed": passed,
        "rules": rules,
        "criteria": judged,
        "judged_by": config.model if config.criteria else None,
        "note": "Rule checks are verified; model judgments are opinions, not proof of correctness.",
    }
    await ctx.emit(
        "review",
        {"passed": passed, "failed_rules": len(failed_rules), "failed": [j["name"] for j in judged if not j["passed"]]},
    )
    return NodeResult(
        outputs={
            "passed": passed,
            "needs_revision": not passed,
            "feedback": feedback,
            "revision_request": revision,
            "report": json.loads(json.dumps(report)),
        },
        branches=frozenset({"pass" if passed else "fail"}),
        usage=usage,
    )

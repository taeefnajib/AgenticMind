"""Loop, Loop End, Router and Join: explicit control-flow constructs.

* **Loop / Loop End** - the nodes wired between them form the loop body, which the runtime executes once
  per iteration (``for_each`` over a list, optionally in parallel, or ``repeat`` with state until a
  condition or ``max_iterations``). Values leave the body only through Loop End.
* **Router** - activates one named route (control branch) chosen by rules or by an LLM.
* **Join** - waits for *all* incoming control paths and combines the values that arrived.
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
    TransientNodeError,
    define_node,
    secret_field,
    ui,
    value_matches_type,
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
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .control import evaluate
from .io import PACKAGE

ValueType = Literal["Any", "String", "Number", "Boolean", "JSON"]


# ---- Loop ---------------------------------------------------------------------------------------
class LoopConfig(NodeConfig):
    mode: Literal["for_each", "repeat"] = Field(
        default="for_each",
        description="for_each: run the body once per item of a list. repeat: run it again and again, passing "
        "the result on as state, until Loop End's 'continue' is false or max iterations is reached.",
    )
    item_type: ValueType = Field(default="Any", description="Type of each item (for_each).")
    state_type: ValueType = Field(default="Any", description="Type of the state (repeat).")
    max_items: int = Field(default=100, ge=1, le=10_000, description="Largest list a for_each loop accepts.")
    on_too_many: Literal["fail", "truncate"] = Field(default="fail", json_schema_extra=ui(advanced=True))
    concurrency: int = Field(
        default=1, ge=1, le=16, description="Items processed at the same time (for_each). 1 = one after another."
    )
    on_item_error: Literal["fail", "skip"] = Field(
        default="fail",
        description="fail: stop the loop on the first failing item. skip: record it and continue.",
        json_schema_extra=ui(advanced=True),
    )
    max_iterations: int = Field(default=5, ge=1, le=100, description="Upper bound for repeat loops.")


@define_node(
    type="agenticmind.core.loop",
    version="1.0.0",
    display_name="Loop",
    category="control",
    package=PACKAGE,
    description="Starts a loop. Wire the body between Loop and Loop End; it runs once per item or iteration.",
    inputs=[
        InputPort(name="items", type=PortType.JSON, required=False, description="List to iterate (for_each)"),
        InputPort(name="initial", type=PortType.ANY, required=False, description="Starting state (repeat)"),
    ],
    outputs=[
        OutputPort(name="item", type=PortType.ANY, type_from_config="item_type", description="Current item"),
        OutputPort(name="index", type=PortType.NUMBER, description="0-based iteration number"),
        OutputPort(name="state", type=PortType.ANY, type_from_config="state_type", description="Current state"),
        OutputPort(name="scope", type=PortType.LOOP_SCOPE, description="Connect to Loop End"),
    ],
    config=LoopConfig,
    runtime_role="loop_start",
    tags=["control", "loop"],
)
async def loop(ctx: NodeContext, inputs, config: LoopConfig) -> NodeResult:
    spec: dict[str, Any] = {"kind": "loop_scope", "mode": config.mode}
    if config.mode == "for_each":
        items = inputs.get("items")
        if items is None:
            raise NodeError("LOOP_ITEMS_MISSING", "A for_each loop needs a list on its 'items' input")
        if not isinstance(items, list):
            raise NodeError("LOOP_ITEMS_INVALID", f"'items' must be a list, got {type(items).__name__}")
        if len(items) > config.max_items:
            if config.on_too_many == "fail":
                raise NodeError("LOOP_TOO_MANY_ITEMS", f"{len(items)} items exceed the limit of {config.max_items}")
            items = items[: config.max_items]
        expected = PortType(config.item_type)
        for i, item in enumerate(items):
            if not value_matches_type(item, expected):
                raise NodeError("LOOP_ITEM_TYPE_MISMATCH", f"Item {i} is not of type {config.item_type}")
        spec.update(items=items, concurrency=config.concurrency, on_item_error=config.on_item_error)
        first: Any = items[0] if items else None
        return NodeResult(outputs={"item": first, "index": 0, "state": None, "scope": spec})
    initial = inputs.get("initial")
    if initial is not None and not value_matches_type(initial, PortType(config.state_type)):
        raise NodeError("LOOP_STATE_TYPE_MISMATCH", f"'initial' is not of type {config.state_type}")
    spec.update(initial=initial, max_iterations=config.max_iterations)
    return NodeResult(outputs={"item": initial, "index": 0, "state": initial, "scope": spec})


_LOOP_END_INPUTS = [
    InputPort(name="scope", type=PortType.LOOP_SCOPE, description="From the Loop's scope output"),
    InputPort(name="value", type=PortType.ANY, description="This iteration's result"),
    InputPort(name="continue", type=PortType.BOOLEAN, required=False, description="repeat: false stops"),
]
_LOOP_END_OUTPUTS = [
    OutputPort(name="results", type=PortType.JSON, description="Every iteration's value, in order"),
    OutputPort(name="last", type=PortType.ANY, description="The final value"),
    OutputPort(name="report", type=PortType.JSON, description="{mode, iterations, stop_reason, errors}"),
]


async def _loop_end(ctx: NodeContext, inputs, config) -> NodeResult:
    raise NodeError("LOOP_END_STANDALONE", "Loop End is executed by the runtime together with its Loop")


_LOOP_END: dict[str, Any] = {
    "type": "agenticmind.core.loop_end",
    "display_name": "Loop End",
    "category": "control",
    "package": PACKAGE,
    "outputs": _LOOP_END_OUTPUTS,
    "runtime_role": "loop_end",
}
#: 1.1.0 adds ``next_state``: in repeat mode the next iteration's state (default: ``value``), so a loop can
#: collect drafts in ``results`` while passing e.g. a revision request forward.
loop_end = define_node(
    **_LOOP_END,
    version="1.1.0",
    description="Ends a loop and collects each iteration's value. repeat: 'continue' false stops; "
    "'next_state' (optional) becomes the next iteration's state.",
    inputs=[
        *_LOOP_END_INPUTS,
        InputPort(
            name="next_state",
            type=PortType.ANY,
            required=False,
            description="repeat: state for the next iteration (default: value)",
        ),
    ],
    tags=["control", "loop"],
)(_loop_end)
loop_end_v1 = define_node(
    **_LOOP_END,
    version="1.0.0",
    description="Ends a loop: collects each iteration's value. In repeat mode a false 'continue' stops the loop.",
    inputs=_LOOP_END_INPUTS,
    tags=["control", "loop", "legacy"],
)(_loop_end)


# ---- Router -------------------------------------------------------------------------------------
class Route(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = ""
    operator: Literal[
        "equals", "not_equals", "contains", "regex", "greater_than", "less_than", "is_empty", "is_truthy"
    ] = "contains"
    value: JsonValue = None


DEFAULT_ROUTES = [
    {
        "name": "billing",
        "description": "Invoices, refunds, payments, charges",
        "operator": "regex",
        "value": "invoice|refund|charge|payment|billing",
    },
    {
        "name": "technical",
        "description": "Bugs, errors, crashes, login or access problems",
        "operator": "regex",
        "value": "error|bug|crash|login|password|cannot|can't",
    },
]


class RouterConfig(NodeConfig):
    mode: Literal["rules", "llm"] = Field(
        default="rules", description="rules: first matching route wins. llm: a model picks the best route."
    )
    routes: list[Route] = Field(
        default=[Route.model_validate(r) for r in DEFAULT_ROUTES],  # plain default so it appears in the schema
        description="Each route becomes an output branch. Unmatched input goes to 'default'.",
    )
    case_sensitive: bool = Field(default=False, json_schema_extra=ui(advanced=True))
    provider: ChatProviderName = Field(default="openrouter", json_schema_extra=ui(advanced=True))
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, json_schema_extra=ui(advanced=True))
    api_key: SecretRef | None = secret_field("API key for llm mode, e.g. OPENROUTER_API_KEY")
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> RouterConfig:
        names = [r.name for r in self.routes]
        if not names:
            raise ValueError("define at least one route")
        if len(names) != len(set(names)):
            raise ValueError("route names must be unique")
        if "default" in names:
            raise ValueError("'default' is reserved for unmatched input")
        if self.mode == "llm":
            check_chat_provider(self.provider, self.api_key, self.base_url)
        return self


def _rule_matches(route: Route, value: Any, case_sensitive: bool) -> bool:
    if route.operator == "regex":
        if not isinstance(route.value, str):
            return False
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(route.value, text, flags) is not None
        except re.error as exc:
            raise NodeError("ROUTER_INVALID_REGEX", f"Route '{route.name}': invalid regex: {exc}") from exc
    if route.operator == "contains" and not case_sensitive and isinstance(route.value, str):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return route.value.lower() in text.lower()
    return evaluate(route.operator, value, route.value)


@define_node(
    type="agenticmind.core.router",
    version="1.0.0",
    display_name="Router",
    category="control",
    package=PACKAGE,
    description="Sends execution down one named route, chosen by rules or by an LLM; 'default' when nothing fits.",
    inputs=[InputPort(name="value", type=PortType.ANY, description="What to route on")],
    outputs=[
        OutputPort(name="route", type=PortType.STRING, description="Name of the chosen route"),
        OutputPort(name="reason", type=PortType.STRING),
    ],
    branches=["default"],
    branches_from_config="routes",
    config=RouterConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=60, max_attempts=2, idempotent=True),
    tags=["control"],
)
async def router(ctx: NodeContext, inputs, config: RouterConfig) -> NodeResult:
    value = inputs["value"]
    if config.mode == "rules":
        for route in config.routes:
            if _rule_matches(route, value, config.case_sensitive):
                reason = f"matched rule {route.operator} {json.dumps(route.value, ensure_ascii=False)}"
                return _routed(route.name, reason)
        return _routed("default", "no rule matched")

    names = [r.name for r in config.routes]
    listing = "\n".join(f"- {r.name}: {r.description or r.name}" for r in config.routes)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    request = ChatRequest(
        model=config.model,
        system="You route requests. Choose exactly one route by calling choose_route. Use 'default' only if no "
        "route fits.\n\nRoutes:\n" + listing,
        messages=[ChatMessage(role="user", content=text)],
        temperature=0,
        max_tokens=1024,  # reasoning models think before calling choose_route
        tools=[
            ToolSpec(
                name="choose_route",
                description="Select the route for this input",
                parameters={
                    "type": "object",
                    "properties": {
                        "route": {"type": "string", "enum": [*names, "default"]},
                        "reason": {"type": "string", "description": "One short sentence"},
                    },
                    "required": ["route"],
                },
            )
        ],
        tool_choice="required",
    )
    try:
        api_key = ctx.secret(config.api_key) if config.api_key else None
        response = await get_provider(config.provider).chat(request, api_key=api_key, base_url=config.base_url)
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc
    choice, reason = None, ""
    for call in response.tool_calls:
        if call.name == "choose_route":
            choice = call.arguments.get("route")
            reason = str(call.arguments.get("reason", ""))
            break
    if choice is None:  # some models answer in text; accept an exact route name
        candidate = response.text.strip().strip(".").lower()
        choice = candidate if candidate in names else None
    if choice not in names:
        await ctx.emit("router_fallback", {"returned": choice})
        return NodeResult(
            outputs={"route": "default", "reason": f"model chose '{choice}', not a known route"},
            branches=frozenset({"default"}),
            usage=response.usage,
        )
    return NodeResult(
        outputs={"route": choice, "reason": reason or "chosen by model"},
        branches=frozenset({choice}),
        usage=response.usage,
    )


def _routed(name: str, reason: str) -> NodeResult:
    return NodeResult(outputs={"route": name, "reason": reason}, branches=frozenset({name}))


# ---- Join ---------------------------------------------------------------------------------------
class JoinConfig(NodeConfig):
    mode: Literal["collect", "merge", "concat"] = Field(
        default="collect",
        description="collect: list of values. merge: combine objects (later keys win). concat: join as text.",
    )
    separator: str = Field(
        default="\n\n", description="Text between values (concat).", json_schema_extra=ui(advanced=True)
    )
    min_values: int = Field(
        default=1, ge=0, le=1000, description="Fail if fewer values arrive (branches that were skipped send none)."
    )


@define_node(
    type="agenticmind.core.join",
    version="1.0.0",
    display_name="Join",
    category="control",
    package=PACKAGE,
    description="Waits for all incoming control paths, then combines the values from parallel branches.",
    inputs=[
        InputPort(name="values", type=PortType.ANY, required=False, cardinality=Cardinality.MULTIPLE),
    ],
    outputs=[
        OutputPort(name="value", type=PortType.JSON),
        OutputPort(name="count", type=PortType.NUMBER, description="How many values arrived"),
    ],
    config=JoinConfig,
    control_join="all",
    tags=["control"],
)
async def join(ctx: NodeContext, inputs, config: JoinConfig) -> NodeResult:
    values = list(inputs.get("values", []))
    if len(values) < config.min_values:
        raise NodeError("JOIN_INCOMPLETE", f"Expected at least {config.min_values} value(s), got {len(values)}")
    if config.mode == "merge":
        merged: dict[str, Any] = {}
        for v in values:
            if not isinstance(v, dict):
                raise NodeError("JOIN_MERGE_NOT_OBJECT", "merge mode needs every value to be an object")
            merged.update(v)
        result: Any = merged
    elif config.mode == "concat":
        result = config.separator.join(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in values)
    else:
        result = values
    return NodeResult(outputs={"value": result, "count": len(values)})

"""Control-flow nodes."""

from __future__ import annotations

from typing import Any, Literal

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
)
from pydantic import Field, JsonValue

from .io import PACKAGE


class ConditionalConfig(NodeConfig):
    operator: Literal[
        "equals", "not_equals", "contains", "greater_than", "less_than", "is_empty", "is_truthy"
    ] = "is_truthy"
    operand: JsonValue = Field(default=None, description="Right-hand side (ignored by is_empty / is_truthy).")


def evaluate(operator: str, value: Any, operand: Any) -> bool:
    match operator:
        case "equals":
            return value == operand
        case "not_equals":
            return value != operand
        case "contains":
            if isinstance(value, (str, list, dict)):
                return operand in value
            return False
        case "greater_than" | "less_than":
            numeric = (int, float)
            if not (
                isinstance(value, numeric)
                and isinstance(operand, numeric)
                and not isinstance(value, bool)
                and not isinstance(operand, bool)
            ):
                raise NodeError("CONDITION_TYPE_ERROR", f"'{operator}' needs numeric value and operand")
            return value > operand if operator == "greater_than" else value < operand
        case "is_empty":
            return value is None or (isinstance(value, (str, list, dict)) and len(value) == 0)
        case "is_truthy":
            return bool(value)
    raise NodeError("CONDITION_UNKNOWN_OPERATOR", operator)


@define_node(
    type="agenticmind.core.conditional",
    version="1.0.0",
    display_name="Conditional",
    category="control",
    package=PACKAGE,
    description="Evaluates a condition and activates the `true` or `false` control branch.",
    inputs=[InputPort(name="value", type=PortType.ANY)],
    outputs=[OutputPort(name="result", type=PortType.BOOLEAN)],
    branches=["true", "false"],
    config=ConditionalConfig,
)
async def conditional(ctx: NodeContext, inputs, config: ConditionalConfig) -> NodeResult:
    result = evaluate(config.operator, inputs["value"], config.operand)
    return NodeResult(outputs={"result": result}, branches=frozenset({"true" if result else "false"}))

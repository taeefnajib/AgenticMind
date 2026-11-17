"""Graph entry and exit nodes."""

from __future__ import annotations

from typing import Literal

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
    ui,
    value_matches_type,
)
from pydantic import Field, JsonValue

PACKAGE = "agenticmind-nodes-core"


class ManualInputConfig(NodeConfig):
    key: str = Field(
        default="input",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
        description="Name of the run input field this node reads.",
    )
    value_type: Literal["String", "Number", "Boolean", "JSON"] = Field(
        default="String", description="Type of the value produced."
    )
    default: JsonValue = Field(default=None, description="Used when the run input omits this field.")
    required: bool = Field(default=True, description="Fail the run if no value and no default.")


@define_node(
    type="agenticmind.core.manual_input",
    version="1.0.0",
    display_name="Manual Input",
    category="input",
    package=PACKAGE,
    description="Reads one field from the run input payload.",
    outputs=[OutputPort(name="value", type=PortType.STRING, type_from_config="value_type")],
    config=ManualInputConfig,
    tags=["entry"],
)
async def manual_input(ctx: NodeContext, inputs, config: ManualInputConfig) -> NodeResult:
    value = ctx.run_input.get(config.key, config.default)
    if value is None:
        if config.required:
            raise NodeError("INPUT_MISSING", f"Run input field '{config.key}' is required")
        value = {"String": "", "Number": 0, "Boolean": False, "JSON": None}[config.value_type]
    if not value_matches_type(value, PortType(config.value_type)):
        raise NodeError(
            "INPUT_TYPE_MISMATCH", f"Run input field '{config.key}' must be of type {config.value_type}"
        )
    return NodeResult(outputs={"value": value})


class OutputConfig(NodeConfig):
    key: str = Field(
        default="output",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
        description="Field name in the run's output payload.",
    )


@define_node(
    type="agenticmind.core.output",
    version="1.0.0",
    display_name="Output",
    category="output",
    package=PACKAGE,
    description="Publishes its input as a field of the run output.",
    inputs=[InputPort(name="value", type=PortType.ANY)],
    config=OutputConfig,
    tags=["exit"],
)
async def output(ctx: NodeContext, inputs, config: OutputConfig) -> NodeResult:
    return NodeResult(run_outputs={config.key: inputs["value"]})


class DelayConfig(NodeConfig):
    seconds: float = Field(default=1.0, ge=0, le=300, json_schema_extra=ui(advanced=True))


@define_node(
    type="agenticmind.core.delay",
    version="1.0.0",
    display_name="Delay",
    category="control",
    package=PACKAGE,
    description="Waits, then passes its input through. Useful for testing timeouts and cancellation.",
    inputs=[InputPort(name="value", type=PortType.ANY, required=False)],
    outputs=[OutputPort(name="value", type=PortType.ANY)],
    config=DelayConfig,
    modes=None,
)
async def delay(ctx: NodeContext, inputs, config: DelayConfig) -> NodeResult:
    import asyncio

    await asyncio.sleep(config.seconds)
    return NodeResult(outputs={"value": inputs.get("value")})

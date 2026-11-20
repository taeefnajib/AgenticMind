"""Graph entry and exit nodes."""

from __future__ import annotations

from typing import Any, Literal

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    define_node,
    image_ref,
    ui,
    value_matches_type,
)
from agenticmind_node_sdk.images import is_image_media_type
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
        raise NodeError("INPUT_TYPE_MISMATCH", f"Run input field '{config.key}' must be of type {config.value_type}")
    return NodeResult(outputs={"value": value})


class ImageInputConfig(NodeConfig):
    key: str = Field(
        default="image",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
        description="Field name in the run input that holds the image (an uploaded file ID).",
    )
    default: list[str] = Field(
        default_factory=list,
        max_length=1,
        description="Image used when the run input does not provide one.",
        json_schema_extra=ui(widget="artifacts"),
    )
    required: bool = True


def _image_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and isinstance(value.get("artifact_id"), str):
        return value["artifact_id"]
    if isinstance(value, list) and value:
        return _image_id(value[0])
    return None


@define_node(
    type="agenticmind.core.image_input",
    version="1.0.0",
    display_name="Image Input",
    category="input",
    package=PACKAGE,
    description="An image given when the graph runs (upload it in Run inputs or Chat), for vision models or OCR.",
    outputs=[OutputPort(name="image", type=PortType.IMAGE)],
    config=ImageInputConfig,
    permissions=Permissions(artifacts=True),
    tags=["entry", "vision"],
)
async def image_input(ctx: NodeContext, inputs, config: ImageInputConfig) -> NodeResult:
    artifact_id = _image_id(ctx.run_input.get(config.key)) or (config.default[0] if config.default else None)
    if artifact_id is None:
        if config.required:
            raise NodeError("INPUT_MISSING", f"Run input field '{config.key}' needs an image (upload one)")
        raise NodeError("INPUT_MISSING", f"No image for '{config.key}'")
    try:
        info = ctx.artifact_info(artifact_id)
    except NodeError:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown or foreign file id
        raise NodeError("INPUT_TYPE_MISMATCH", f"Run input field '{config.key}' is not an uploaded file") from exc
    if not is_image_media_type(info.media_type):
        raise NodeError("INPUT_TYPE_MISMATCH", f"{info.filename} is not an image ({info.media_type})")
    return NodeResult(outputs={"image": image_ref(info)})


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

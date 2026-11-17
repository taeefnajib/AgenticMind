"""Transformation and explicit type-conversion nodes."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from agenticmind_node_sdk import (
    Cardinality,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
    is_json_value,
    ui,
)
from pydantic import Field

from .io import PACKAGE

_MISSING = object()
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}")


def lookup_path(data: Any, path: str) -> Any:
    """Resolve a dotted path (``a.b.0.c``) in nested dicts/lists. Returns ``_MISSING`` if absent."""
    current = data
    if path == "":
        return current
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


class TextTemplateConfig(NodeConfig):
    template: str = Field(
        default="Hello, {{ text }}!",
        description="Use {{ text }} for the text input and {{ name }} / {{ a.b }} for fields of `variables`.",
        json_schema_extra=ui(widget="textarea"),
    )
    strict: bool = Field(
        default=True,
        description="Fail when a placeholder cannot be resolved (otherwise it renders empty).",
        json_schema_extra=ui(advanced=True),
    )


@define_node(
    type="agenticmind.core.text_template",
    version="1.0.0",
    display_name="Text Template",
    category="transform",
    package=PACKAGE,
    description="Renders a text template with {{ placeholders }}.",
    inputs=[
        InputPort(name="text", type=PortType.STRING, required=False, description="Available as {{ text }}"),
        InputPort(name="variables", type=PortType.JSON, required=False, description="Object of named values"),
    ],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=TextTemplateConfig,
)
async def text_template(ctx: NodeContext, inputs, config: TextTemplateConfig) -> NodeResult:
    scope: dict[str, Any] = {}
    variables = inputs.get("variables")
    if isinstance(variables, dict):
        scope.update(variables)
    elif variables is not None:
        scope["variables"] = variables
    if "text" in inputs:
        scope["text"] = inputs["text"]

    def replace(match: re.Match[str]) -> str:
        value = lookup_path(scope, match.group(1))
        if value is _MISSING:
            if config.strict:
                raise NodeError("TEMPLATE_MISSING_VARIABLE", f"Template variable '{match.group(1)}' not found")
            return ""
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    return NodeResult(outputs={"text": _TEMPLATE_RE.sub(replace, config.template)})


class TextTransformConfig(NodeConfig):
    operation: Literal["uppercase", "lowercase", "trim", "title", "slugify"] = "uppercase"


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


_TEXT_OPS = {
    "uppercase": str.upper,
    "lowercase": str.lower,
    "trim": str.strip,
    "title": str.title,
    "slugify": _slugify,
}


@define_node(
    type="agenticmind.core.text_transform",
    version="1.0.0",
    display_name="Text Transform",
    category="transform",
    package=PACKAGE,
    description="Applies a simple text operation.",
    inputs=[InputPort(name="text", type=PortType.STRING)],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=TextTransformConfig,
)
async def text_transform(ctx: NodeContext, inputs, config: TextTransformConfig) -> NodeResult:
    return NodeResult(outputs={"text": _TEXT_OPS[config.operation](inputs["text"])})


class JsonSelectConfig(NodeConfig):
    path: str = Field(default="", description="Dotted path, e.g. `user.addresses.0.city`. Empty = whole value.")
    required: bool = Field(default=True, description="Fail if the path does not exist (otherwise null).")


@define_node(
    type="agenticmind.core.json_select",
    version="1.0.0",
    display_name="JSON Select",
    category="transform",
    package=PACKAGE,
    description="Selects a value inside a JSON document.",
    inputs=[InputPort(name="data", type=PortType.JSON)],
    outputs=[OutputPort(name="value", type=PortType.JSON)],
    config=JsonSelectConfig,
)
async def json_select(ctx: NodeContext, inputs, config: JsonSelectConfig) -> NodeResult:
    value = lookup_path(inputs["data"], config.path)
    if value is _MISSING:
        if config.required:
            raise NodeError("JSON_PATH_NOT_FOUND", f"Path '{config.path}' not found")
        value = None
    return NodeResult(outputs={"value": value})


class ToTextConfig(NodeConfig):
    pretty: bool = Field(default=False, description="Indent JSON output.")


@define_node(
    type="agenticmind.core.to_text",
    version="1.0.0",
    display_name="To Text",
    category="convert",
    package=PACKAGE,
    description="Explicitly converts any value to text (strings pass through; others become JSON).",
    inputs=[InputPort(name="value", type=PortType.ANY)],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=ToTextConfig,
)
async def to_text(ctx: NodeContext, inputs, config: ToTextConfig) -> NodeResult:
    value = inputs["value"]
    if isinstance(value, str):
        return NodeResult(outputs={"text": value})
    if not is_json_value(value):
        raise NodeError("CONVERSION_FAILED", "Value is not JSON-serialisable")
    return NodeResult(
        outputs={"text": json.dumps(value, ensure_ascii=False, indent=2 if config.pretty else None)}
    )


@define_node(
    type="agenticmind.core.parse_json",
    version="1.0.0",
    display_name="Parse JSON",
    category="convert",
    package=PACKAGE,
    description="Explicitly parses text into a JSON value.",
    inputs=[InputPort(name="text", type=PortType.STRING)],
    outputs=[OutputPort(name="value", type=PortType.JSON)],
)
async def parse_json(ctx: NodeContext, inputs, config) -> NodeResult:
    try:
        value = json.loads(inputs["text"])
    except json.JSONDecodeError as exc:
        raise NodeError("CONVERSION_FAILED", f"Invalid JSON: {exc}") from exc
    return NodeResult(outputs={"value": value})


@define_node(
    type="agenticmind.core.collect",
    version="1.0.0",
    display_name="Collect",
    category="transform",
    package=PACKAGE,
    description="Joins several branches into one list (explicit join; order follows edge IDs).",
    inputs=[InputPort(name="items", type=PortType.ANY, cardinality=Cardinality.MULTIPLE)],
    outputs=[OutputPort(name="items", type=PortType.JSON)],
)
async def collect(ctx: NodeContext, inputs, config) -> NodeResult:
    items = list(inputs.get("items", []))
    if not is_json_value(items):
        raise NodeError("CONVERSION_FAILED", "Collected items are not JSON-serialisable")
    return NodeResult(outputs={"items": items})

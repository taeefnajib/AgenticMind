from typing import Literal

import pytest
from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretAccessError,
    SecretRef,
    define_node,
    is_compatible,
    secret_field,
    secret_fields,
    value_matches_type,
)
from agenticmind_node_sdk.testing import invoke_node
from pydantic import ValidationError


class EchoConfig(NodeConfig):
    suffix: str = "!"
    out_type: Literal["String", "JSON"] = "String"


@define_node(
    type="tests.sdk.echo",
    version="1.0.0",
    display_name="Echo",
    category="test",
    package="tests",
    inputs=[InputPort(name="text", type=PortType.STRING)],
    outputs=[OutputPort(name="text", type=PortType.STRING, type_from_config="out_type")],
    config=EchoConfig,
)
async def echo(ctx: NodeContext, inputs, config: EchoConfig) -> NodeResult:
    return NodeResult(outputs={"text": inputs["text"] + config.suffix})


class KeyConfig(NodeConfig):
    api_key: SecretRef | None = secret_field("Key")


def make_secret_node(credentials: bool):
    @define_node(
        type="tests.sdk.secret",
        version="1.0.0",
        display_name="Secret",
        category="test",
        package="tests",
        outputs=[OutputPort(name="value", type=PortType.STRING)],
        config=KeyConfig,
        permissions=Permissions(credentials=credentials),
    )
    async def handler(ctx: NodeContext, inputs, config: KeyConfig) -> NodeResult:
        assert config.api_key is not None
        return NodeResult(outputs={"value": ctx.secret(config.api_key)[:3]})

    return handler


def test_compatibility_rules():
    assert is_compatible(PortType.STRING, PortType.STRING)
    assert is_compatible(PortType.STRING, PortType.ANY)
    assert is_compatible(PortType.NUMBER, PortType.JSON)
    assert not is_compatible(PortType.JSON, PortType.STRING)
    assert not is_compatible(PortType.ANY, PortType.STRING)
    assert not is_compatible(PortType.STRING, PortType.NUMBER)


def test_value_checks():
    assert value_matches_type(1, PortType.NUMBER)
    assert not value_matches_type(True, PortType.NUMBER)
    assert value_matches_type({"a": [1, None]}, PortType.JSON)
    assert not value_matches_type(float("nan"), PortType.JSON)
    assert not value_matches_type(object(), PortType.JSON)


def test_manifest_contains_config_schema():
    m = echo.manifest
    assert m.config_schema["properties"]["suffix"]["default"] == "!"
    assert m.branches == ["done"]


async def test_invoke_node_outside_ui():
    result = await invoke_node(echo, inputs={"text": "hi"}, config={"suffix": "?"})
    assert result.outputs == {"text": "hi?"}


async def test_output_type_check_respects_type_from_config():
    # declared String output but config says JSON: a str is still valid JSON
    await invoke_node(echo, inputs={"text": "hi"}, config={"out_type": "JSON"})


def test_unknown_config_key_rejected():
    with pytest.raises(ValidationError):
        echo.parse_config({"nope": 1})


def test_manifest_rejects_bad_type_from_config():
    with pytest.raises(ValidationError):

        @define_node(
            type="tests.sdk.bad",
            version="1.0.0",
            display_name="Bad",
            category="test",
            package="tests",
            outputs=[OutputPort(name="x", type=PortType.ANY, type_from_config="missing")],
        )
        async def bad(ctx, inputs, config):  # pragma: no cover
            return NodeResult()


async def test_secret_requires_permission():
    cfg = {"api_key": {"$secret": "MY_KEY"}}
    assert secret_fields(make_secret_node(True).manifest.config_schema) == {"api_key"}
    ok = await invoke_node(make_secret_node(True), config=cfg, secrets={"MY_KEY": "abcdef"})
    assert ok.outputs["value"] == "abc"
    with pytest.raises(SecretAccessError):
        await invoke_node(make_secret_node(False), config=cfg, secrets={"MY_KEY": "abcdef"})


async def test_missing_output_detected():
    @define_node(
        type="tests.sdk.lazy",
        version="1.0.0",
        display_name="Lazy",
        category="test",
        package="tests",
        outputs=[OutputPort(name="x", type=PortType.STRING)],
    )
    async def lazy(ctx, inputs, config):
        return NodeResult()

    with pytest.raises(NodeError) as exc:
        await invoke_node(lazy)
    assert exc.value.code == "NODE_OUTPUT_MISSING"

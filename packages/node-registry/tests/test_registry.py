import pytest
from agenticmind_node_sdk import NodeResult, define_node
from agenticmind_registry import NodeRegistry, RegistryError, TrustLevel, default_registry, semver_key


def make(version: str):
    @define_node(type="tests.reg.node", version=version, display_name="N", category="t", package="tests")
    async def h(ctx, inputs, config):
        return NodeResult()

    return h


def test_versions_sorted_semantically():
    reg = NodeRegistry()
    for v in ["1.10.0", "1.2.0", "1.2.0-beta", "0.9.0"]:
        reg.register(make(v))
    assert reg.versions("tests.reg.node") == ["0.9.0", "1.2.0-beta", "1.2.0", "1.10.0"]
    assert reg.latest("tests.reg.node").definition.manifest.version == "1.10.0"
    assert semver_key("2.0.0") > semver_key("1.99.99")


def test_duplicate_registration_rejected():
    reg = NodeRegistry()
    reg.register(make("1.0.0"))
    with pytest.raises(RegistryError):
        reg.register(make("1.0.0"))


def test_default_trust_is_private_for_manual_registration():
    reg = NodeRegistry()
    reg.register(make("1.0.0"))
    assert reg.get("tests.reg.node", "1.0.0").trust == TrustLevel.PRIVATE


def test_entry_point_discovery_assigns_core_trust():
    reg = default_registry()
    entry = reg.latest("agenticmind.core.output")
    assert entry is not None
    assert entry.trust == TrustLevel.CORE
    assert entry.source == "agenticmind-nodes-core"

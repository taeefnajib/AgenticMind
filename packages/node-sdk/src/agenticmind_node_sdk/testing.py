"""Helpers to test node handlers in isolation, without the UI, API or runtime scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from agenticmind_ir import iter_secret_refs

from .context import NodeContext, NodeResult
from .define import NodeDefinition
from .errors import NodeError
from .manifest import resolve_port_type
from .types import value_matches_type


async def invoke_node(
    definition: NodeDefinition[Any],
    *,
    inputs: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    run_input: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    check_outputs: bool = True,
) -> NodeResult:
    raw_config = dict(config or {})
    parsed = definition.parse_config(raw_config)
    allowed = (
        frozenset(iter_secret_refs(raw_config)) if definition.manifest.permissions.credentials else frozenset()
    )
    ctx = NodeContext(
        run_id="test-run",
        project_id="test-project",
        node_id="node_under_test",
        invocation_id="inv-1",
        attempt=1,
        run_input=dict(run_input or {}),
        allowed_secrets=allowed,
        _secret_lookup=(secrets or {}).get,
    )
    result = await definition.handler(ctx, dict(inputs or {}), parsed)
    if check_outputs:
        for port in definition.manifest.outputs:
            if port.name not in result.outputs:
                raise NodeError("NODE_OUTPUT_MISSING", f"Missing output {port.name}")
            ptype = resolve_port_type(port, raw_config)
            if not value_matches_type(result.outputs[port.name], ptype):
                raise NodeError("NODE_OUTPUT_TYPE_MISMATCH", f"Output {port.name} is not {ptype.value}")
    return result


def invoke_node_sync(definition: NodeDefinition[Any], **kwargs: Any) -> NodeResult:
    return asyncio.run(invoke_node(definition, **kwargs))

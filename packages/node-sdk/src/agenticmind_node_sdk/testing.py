"""Helpers to test node handlers in isolation, without the UI, API or runtime scheduler."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agenticmind_ir import iter_secret_refs

from .artifacts import ArtifactInfo, ArtifactNotFoundError, ArtifactReader
from .context import NodeContext, NodeResult
from .define import NodeDefinition
from .errors import NodeError
from .manifest import resolve_port_type
from .memory import MemoryAccess
from .types import value_matches_type


async def invoke_node(
    definition: NodeDefinition[Any],
    *,
    inputs: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    run_input: Mapping[str, Any] | None = None,
    secrets: Mapping[str, str] | None = None,
    check_outputs: bool = True,
    artifacts: ArtifactReader | None = None,
    storage_root: Path | None = None,
    allowed_secrets: frozenset[str] | None = None,
    memory: MemoryAccess | None = None,
) -> NodeResult:
    raw_config = dict(config or {})
    parsed = definition.parse_config(raw_config)
    allowed = frozenset(iter_secret_refs(raw_config)) if definition.manifest.permissions.credentials else frozenset()
    ctx = NodeContext(
        run_id="test-run",
        project_id="test-project",
        node_id="node_under_test",
        invocation_id="inv-1",
        attempt=1,
        run_input=dict(run_input or {}),
        allowed_secrets=allowed | (allowed_secrets or frozenset()),
        _secret_lookup=(secrets or {}).get,
        _artifacts=artifacts if definition.manifest.permissions.artifacts else None,
        _storage_root=storage_root if definition.manifest.permissions.filesystem else None,
        _memory=memory if definition.manifest.permissions.memory else None,
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


class MemoryArtifacts:
    """In-memory ``ArtifactReader`` for node tests: ``MemoryArtifacts({"a1": ("doc.txt", b"hi")})``."""

    def __init__(self, files: Mapping[str, tuple[str, bytes]]) -> None:
        import hashlib
        import mimetypes

        self._files = dict(files)
        self._info = {
            aid: ArtifactInfo(
                id=aid,
                filename=name,
                media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            for aid, (name, data) in self._files.items()
        }

    def info(self, artifact_id: str) -> ArtifactInfo:
        if artifact_id not in self._info:
            raise ArtifactNotFoundError(artifact_id)
        return self._info[artifact_id]

    def read_bytes(self, artifact_id: str) -> bytes:
        if artifact_id not in self._files:
            raise ArtifactNotFoundError(artifact_id)
        return self._files[artifact_id][1]

    def list(self) -> list[ArtifactInfo]:
        return list(self._info.values())

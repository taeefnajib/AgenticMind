"""Execution context passed to node handlers, and the result they return."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .artifacts import ArtifactInfo, ArtifactNotFoundError, ArtifactReader
from .config import SecretRef
from .errors import NodeError, SecretAccessError
from .memory import MemoryAccess

NodeInputs = Mapping[str, Any]


class ModelUsage(BaseModel):
    provider: str
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)


@dataclass(frozen=True)
class NodeResult:
    """What a handler returns.

    * ``outputs`` – values for every declared output port.
    * ``branches`` – control branches to activate; ``None`` activates all declared branches
      (for ordinary nodes that is just ``done``). Branching nodes must choose explicitly.
    * ``run_outputs`` – values contributed to the run's final output payload.
    * ``usage`` – model/token usage for observability and cost estimation.
    """

    outputs: Mapping[str, Any] = field(default_factory=dict)
    branches: frozenset[str] | None = None
    run_outputs: Mapping[str, Any] = field(default_factory=dict)
    usage: ModelUsage | None = None


SecretLookup = Callable[[str], str | None]
EventEmitter = Callable[[str, Mapping[str, Any]], Awaitable[None]]


@dataclass
class NodeContext:
    run_id: str
    project_id: str
    node_id: str
    invocation_id: str
    attempt: int
    run_input: Mapping[str, Any]
    allowed_secrets: frozenset[str] = frozenset()
    _secret_lookup: SecretLookup | None = None
    _emitter: EventEmitter | None = None
    #: Receives streamed text deltas (ADR-0040); None when nobody listens.
    _streamer: Callable[[str], Awaitable[None]] | None = None
    _artifacts: ArtifactReader | None = None
    _storage_root: Path | None = None
    _memory: MemoryAccess | None = None
    #: The response supplied when a suspended run is resumed (``None`` on a normal invocation).
    resume_value: Any = None

    def secret(self, ref: SecretRef) -> str:
        """Resolve a secret referenced by this node's own config (requires ``credentials`` permission)."""
        if ref.name not in self.allowed_secrets:
            raise SecretAccessError(
                f"Node {self.node_id} may not access secret {ref.name}: not referenced in its config "
                "or node lacks the 'credentials' permission"
            )
        value = self._secret_lookup(ref.name) if self._secret_lookup else None
        if value is None:
            raise SecretAccessError(f"Secret {ref.name} is not configured in this environment")
        return value

    def _artifact_reader(self) -> ArtifactReader:
        if self._artifacts is None:
            raise NodeError(
                "ARTIFACT_ACCESS_DENIED",
                f"Node {self.node_id} cannot read project files: it lacks the 'artifacts' permission "
                "or no file store is configured",
            )
        return self._artifacts

    def artifact_info(self, artifact_id: str) -> ArtifactInfo:
        try:
            return self._artifact_reader().info(artifact_id)
        except ArtifactNotFoundError as exc:
            raise NodeError("ARTIFACT_NOT_FOUND", f"File '{artifact_id}' does not exist in this project") from exc

    def read_artifact(self, artifact_id: str) -> bytes:
        """Read an uploaded project file (requires ``Permissions(artifacts=True)``)."""
        try:
            return self._artifact_reader().read_bytes(artifact_id)
        except ArtifactNotFoundError as exc:
            raise NodeError("ARTIFACT_NOT_FOUND", f"File '{artifact_id}' does not exist in this project") from exc

    def storage_path(self, *parts: str) -> Path:
        """Persistent local directory for node-managed data (requires ``filesystem`` permission).

        Used for embedded databases and model caches, e.g. ``ctx.storage_path("vectors", "qdrant")``.
        """
        if self._storage_root is None:
            raise NodeError(
                "STORAGE_UNAVAILABLE",
                f"Node {self.node_id} has no local storage: it lacks the 'filesystem' permission "
                "or the runtime has no data directory",
            )
        for part in parts:
            if not part or part in (".", "..") or "/" in part or "\\" in part:
                raise NodeError("STORAGE_INVALID_PATH", f"Invalid storage path segment {part!r}")
        path = self._storage_root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def memory(self) -> MemoryAccess:
        """Project-scoped conversation state and long-term memories (requires ``memory`` permission)."""
        if self._memory is None:
            raise NodeError(
                "MEMORY_UNAVAILABLE",
                f"Node {self.node_id} has no memory access: it lacks the 'memory' permission "
                "or the runtime has no memory store",
            )
        return self._memory

    @property
    def streaming(self) -> bool:
        """True when someone receives ``stream_text`` deltas (nodes may skip streaming otherwise)."""
        return self._streamer is not None

    async def stream_text(self, delta: str) -> None:
        """Stream a piece of this node's text output as it is produced (e.g. model tokens), ADR-0040.

        Deltas are a live preview: the node's outputs remain the source of truth.
        """
        if self._streamer is not None and delta:
            await self._streamer(delta)

    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        """Emit a custom trace event (e.g. ``tool_call``). Data is redacted by the runtime."""
        if self._emitter is not None:
            await self._emitter(event_type, data)

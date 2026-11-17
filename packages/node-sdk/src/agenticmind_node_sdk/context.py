"""Execution context passed to node handlers, and the result they return."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from .artifacts import ArtifactInfo, ArtifactNotFoundError, ArtifactReader
from .config import SecretRef
from .errors import NodeError, SecretAccessError

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
    _artifacts: ArtifactReader | None = None

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

    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        """Emit a custom trace event (e.g. ``tool_call``). Data is redacted by the runtime."""
        if self._emitter is not None:
            await self._emitter(event_type, data)

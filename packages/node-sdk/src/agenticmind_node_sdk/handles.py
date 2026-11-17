"""Handles: small, serialisable descriptors of shared resources passed along data edges.

A handle carries configuration (and secret *references*, never values) so that several nodes use
exactly the same resource: e.g. the Index Documents and Retriever nodes share one embedding model and
one vector store collection. A node consuming a handle may resolve the secret references it carries,
provided it declares the ``credentials`` permission (the runtime delegates them along the edge).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import SecretRef

#: Port types whose values are handles (used by the runtime for secret delegation).
HANDLE_PORT_TYPES = frozenset({"EmbeddingModel", "VectorStoreHandle", "Tool"})


class _Handle(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmbeddingModelHandle(_Handle):
    kind: Literal["embedding_model"] = "embedding_model"
    provider: str
    model: str
    dimensions: int | None = Field(default=None, ge=1)
    base_url: str | None = None
    api_key: SecretRef | None = None
    batch_size: int = Field(default=32, ge=1, le=2048)

    @property
    def model_id(self) -> str:
        """Identity recorded with every stored vector, used to detect model mismatches."""
        suffix = f"@{self.dimensions}" if self.dimensions else ""
        return f"{self.provider}:{self.model}{suffix}"


class VectorStoreHandle(_Handle):
    kind: Literal["vector_store"] = "vector_store"
    backend: str
    collection: str
    distance: Literal["cosine", "dot"] = "cosine"
    url: str | None = None
    api_key: SecretRef | None = None
    dsn: SecretRef | None = None
    cloud: str | None = None
    region: str | None = None
    namespace: str | None = None


TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class ToolHandle(_Handle):
    """A tool an agent may call.

    ``tool_type`` selects the implementation; ``parameters`` is the JSON Schema the model must follow
    (arguments are validated against it before execution); ``binding`` holds the tool's own config
    and any handles it needs (e.g. an embedding model and vector store). ``permissions`` lists what
    executing it requires; the calling node must declare them all.
    """

    kind: Literal["tool"] = "tool"
    tool_type: str
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=2000)
    parameters: dict[str, Any]
    binding: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)


def handle_value(handle: _Handle) -> dict[str, Any]:
    return handle.model_dump(mode="json", by_alias=True, exclude_none=True)


def is_handle(value: Any, kind: str) -> bool:
    return isinstance(value, dict) and value.get("kind") == kind

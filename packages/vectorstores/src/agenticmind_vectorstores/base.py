"""Vector store contract shared by all adapters.

Adapters are synchronous (nodes call them via ``asyncio.to_thread``) and store, per vector:
the full document JSON, a content hash (to skip unchanged chunks on re-index), the identity of the
embedding model that produced the vector (to detect model mismatches) and a few flat, filterable
fields (``filename``, ``artifact_id``, ``page``, ``section``, ``parent_id`` and ``meta_<key>`` for
scalar metadata).

Scores are always "higher is more similar": cosine similarity or dot product.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Scalar = str | int | float | bool
Distance = Literal["cosine", "dot"]

FILTER_FIELDS = ("filename", "artifact_id", "page", "section", "parent_id")


class VectorStoreError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class StoreSpec:
    """Resolved connection settings (secret values already resolved by the caller)."""

    backend: str
    collection: str
    distance: Distance = "cosine"
    url: str | None = None
    api_key: str | None = None
    dsn: str | None = None
    cloud: str | None = None
    region: str | None = None
    namespace: str | None = None
    local_path: Path | None = None


@dataclass
class StoreRecord:
    id: str
    vector: list[float]
    document: dict[str, Any]
    content_hash: str
    embedding_model: str
    fields: dict[str, Scalar] = field(default_factory=dict)


@dataclass
class StoreHit:
    id: str
    score: float
    document: dict[str, Any]
    embedding_model: str | None = None


def filter_fields(document: Mapping[str, Any]) -> dict[str, Scalar]:
    """Flat, filterable fields extracted from a document."""
    out: dict[str, Scalar] = {}
    source = document.get("source") or {}
    for key in ("filename", "artifact_id", "page", "section"):
        if isinstance(source.get(key), (str, int, float, bool)):
            out[key] = source[key]
    if isinstance(document.get("parent_id"), str):
        out["parent_id"] = document["parent_id"]
    for key, value in (document.get("metadata") or {}).items():
        if isinstance(value, (str, int, float, bool)) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", key):
            out[f"meta_{key}"] = value
    return out


def normalize_filter(filters: Mapping[str, Scalar] | None) -> dict[str, Scalar]:
    """Accept ``filename`` / ``page`` / ... and ``metadata.<key>``; reject anything else."""
    out: dict[str, Scalar] = {}
    for key, value in (filters or {}).items():
        if key in FILTER_FIELDS:
            out[key] = value
        elif key.startswith("metadata.") and re.fullmatch(r"[A-Za-z0-9_]{1,64}", key[9:]):
            out[f"meta_{key[9:]}"] = value
        else:
            raise VectorStoreError(
                "FILTER_INVALID", f"Unsupported filter field '{key}' (use {', '.join(FILTER_FIELDS)} or metadata.<key>)"
            )
        if not isinstance(value, (str, int, float, bool)):
            raise VectorStoreError("FILTER_INVALID", f"Filter value for '{key}' must be a scalar")
    return out


class VectorStore(ABC):
    def __init__(self, spec: StoreSpec) -> None:
        self.spec = spec

    @abstractmethod
    def exists(self) -> bool: ...

    @abstractmethod
    def ensure_collection(self, dimensions: int) -> None:
        """Create the collection if missing; raise ``EMBEDDING_DIMENSION_MISMATCH`` if incompatible."""

    @abstractmethod
    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        """``{id: (content_hash, embedding_model)}`` for ids already stored."""

    @abstractmethod
    def upsert(self, records: Sequence[StoreRecord]) -> None: ...

    @abstractmethod
    def query(
        self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None
    ) -> list[StoreHit]: ...

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def drop(self) -> None: ...

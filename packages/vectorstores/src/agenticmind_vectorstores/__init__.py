"""AgenticMind vector store adapters."""

from __future__ import annotations

from pathlib import Path

from .base import (
    FILTER_FIELDS,
    StoreHit,
    StoreRecord,
    StoreSpec,
    VectorStore,
    VectorStoreError,
    filter_fields,
    normalize_filter,
)
from .keyword import KeywordHit, KeywordIndex, KeywordRecord, fts_query, index_path

#: Backends and whether they can run fully locally (embedded) without a server.
BACKENDS: dict[str, dict[str, object]] = {
    "qdrant": {"local": True, "label": "Qdrant (embedded local, or server/cloud via URL)"},
    "chroma": {"local": True, "label": "Chroma (embedded local, or server via URL)"},
    "pgvector": {"local": False, "label": "PostgreSQL + pgvector (dsn secret)"},
    "pinecone": {"local": False, "label": "Pinecone serverless (api_key secret)"},
}
DEFAULT_BACKEND = "qdrant"


def open_store(spec: StoreSpec) -> VectorStore:
    """Instantiate the adapter for ``spec.backend`` (imports optional dependencies lazily)."""
    if spec.backend == "qdrant":
        from .qdrant import QdrantStore

        return QdrantStore(spec)
    if spec.backend == "chroma":
        from .chroma import ChromaStore

        return ChromaStore(spec)
    if spec.backend == "pgvector":
        from .pgvector import PgVectorStore

        return PgVectorStore(spec)
    if spec.backend == "pinecone":
        from .pinecone import PineconeStore

        return PineconeStore(spec)
    raise VectorStoreError("STORE_UNKNOWN", f"Unknown vector store backend '{spec.backend}'")


def delete_local_collections(data_dir: Path, prefix: str) -> list[str]:
    """Remove embedded Qdrant/Chroma collections whose names start with ``prefix`` (project cleanup)."""
    removed: list[str] = []
    for backend in ("qdrant", "chroma"):
        path = data_dir / "vectors" / backend
        if not path.is_dir():
            continue
        try:
            store = open_store(StoreSpec(backend=backend, collection=f"{prefix}placeholder", local_path=path))
        except VectorStoreError:
            continue
        for name in store.list_collections():  # type: ignore[attr-defined]
            if name.startswith(prefix):
                open_store(StoreSpec(backend=backend, collection=name, local_path=path)).drop()
                removed.append(f"{backend}:{name}")
    return removed


__all__ = [
    "BACKENDS",
    "DEFAULT_BACKEND",
    "FILTER_FIELDS",
    "KeywordHit",
    "KeywordIndex",
    "KeywordRecord",
    "StoreHit",
    "StoreRecord",
    "StoreSpec",
    "VectorStore",
    "VectorStoreError",
    "delete_local_collections",
    "filter_fields",
    "fts_query",
    "index_path",
    "normalize_filter",
    "open_store",
]

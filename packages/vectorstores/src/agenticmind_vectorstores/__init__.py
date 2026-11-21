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
    "faiss": {"local": True, "label": "FAISS (embedded local files)"},
    "lancedb": {"local": True, "label": "LanceDB (embedded local, or LanceDB Cloud via db:// URL)"},
    "milvus": {"local": False, "label": "Milvus / Zilliz Cloud (URL + token)"},
    "weaviate": {"local": False, "label": "Weaviate (server or Weaviate Cloud URL + api_key)"},
    "elasticsearch": {"local": False, "label": "Elasticsearch (URL + api_key)"},
    "opensearch": {"local": False, "label": "OpenSearch (URL with user:password)"},
    "mongodb": {"local": False, "label": "MongoDB Atlas Vector Search (dsn secret)"},
    "redis": {"local": False, "label": "Redis with the query engine (URL or dsn secret)"},
}
#: Backends that keep collections on the API server's disk when no URL is set.
LOCAL_BACKENDS = ("qdrant", "chroma", "faiss", "lancedb")
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
    if spec.backend == "faiss":
        from .faiss_store import FaissStore

        return FaissStore(spec)
    if spec.backend == "lancedb":
        from .lancedb_store import LanceDBStore

        return LanceDBStore(spec)
    if spec.backend == "milvus":
        from .milvus_store import MilvusStore

        return MilvusStore(spec)
    if spec.backend == "weaviate":
        from .weaviate_store import WeaviateStore

        return WeaviateStore(spec)
    if spec.backend in ("elasticsearch", "opensearch"):
        from .search_store import SearchEngineStore

        return SearchEngineStore(spec)
    if spec.backend == "mongodb":
        from .mongodb_store import MongoDBStore

        return MongoDBStore(spec)
    if spec.backend == "redis":
        from .redis_store import RedisStore

        return RedisStore(spec)
    raise VectorStoreError("STORE_UNKNOWN", f"Unknown vector store backend '{spec.backend}'")


def delete_local_collections(data_dir: Path, prefix: str) -> list[str]:
    """Remove embedded collections whose names start with ``prefix`` (project cleanup)."""
    removed: list[str] = []
    for backend in LOCAL_BACKENDS:
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
    "LOCAL_BACKENDS",
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

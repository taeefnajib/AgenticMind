"""Chroma adapter: embedded persistent mode (local path) or a Chroma server (``url``).

Chroma metadata values must be flat scalars, so the document is stored as a JSON string.
Chroma returns *distances*; for cosine we convert to similarity (``1 - distance``) and for dot
product (``ip`` space, distance ``1 - dot``) likewise.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter

_CLIENTS: dict[str, Any] = {}
_LOCK = threading.Lock()


def _client(spec: StoreSpec) -> Any:
    try:
        import chromadb
    except ImportError as exc:  # pragma: no cover - optional extra
        raise VectorStoreError(
            "STORE_NOT_INSTALLED", "Chroma support needs the 'chromadb' package (agenticmind-vectorstores[chroma])"
        ) from exc
    if spec.url:
        parsed = urlparse(spec.url)
        headers = {"x-chroma-token": spec.api_key} if spec.api_key else None
        return chromadb.HttpClient(
            host=parsed.hostname or "localhost",
            port=parsed.port or (443 if parsed.scheme == "https" else 8000),
            ssl=parsed.scheme == "https",
            headers=headers,
        )
    if spec.local_path is None:
        raise VectorStoreError("STORE_CONFIG_INVALID", "Local Chroma needs a storage path")
    key = str(spec.local_path.resolve())
    with _LOCK:
        if key not in _CLIENTS:
            spec.local_path.mkdir(parents=True, exist_ok=True)
            _CLIENTS[key] = chromadb.PersistentClient(path=key)
        return _CLIENTS[key]


def collection_name(name: str) -> str:
    """Chroma: 3–512 chars of [A-Za-z0-9._-], starting and ending alphanumeric."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._-")[:512]
    if len(clean) < 3:
        clean = (clean + "_col").strip("_") if clean else "collection"
    return clean


class ChromaStore(VectorStore):
    def __init__(self, spec: StoreSpec) -> None:
        super().__init__(spec)
        self.client = _client(spec)
        self.name = collection_name(spec.collection)

    def _get(self) -> Any | None:
        try:
            return self.client.get_collection(self.name, embedding_function=None)
        except Exception:  # noqa: BLE001 - chroma raises NotFoundError/ValueError depending on version
            return None

    def exists(self) -> bool:
        return self._get() is not None

    def ensure_collection(self, dimensions: int) -> None:
        space = "cosine" if self.spec.distance == "cosine" else "ip"
        col = self.client.get_or_create_collection(
            self.name,
            configuration={"hnsw": {"space": space}},
            metadata={"agenticmind_dimensions": dimensions},
            embedding_function=None,
        )
        stored = (col.metadata or {}).get("agenticmind_dimensions")
        if stored is not None and int(stored) != dimensions:
            raise VectorStoreError(
                "EMBEDDING_DIMENSION_MISMATCH",
                f"Collection '{self.spec.collection}' stores {stored}-d vectors but the model produces {dimensions}-d.",
            )

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        col = self._get()
        if col is None or not ids:
            return {}
        got = col.get(ids=list(ids), include=["metadatas"])
        return {
            i: (str(m.get("content_hash", "")), str(m.get("embedding_model", "")))
            for i, m in zip(got["ids"], got["metadatas"] or [], strict=False)
            if m
        }

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        col = self._get()
        if col is None:
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Collection '{self.spec.collection}' does not exist")
        col.upsert(
            ids=[r.id for r in records],
            embeddings=[r.vector for r in records],
            metadatas=[
                {
                    "document": json.dumps(r.document, ensure_ascii=False),
                    "content_hash": r.content_hash,
                    "embedding_model": r.embedding_model,
                    **r.fields,
                }
                for r in records
            ],
        )

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        col = self._get()
        if col is None:
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Collection '{self.spec.collection}' does not exist")
        flt = normalize_filter(filters)
        where: dict[str, Any] | None
        if not flt:
            where = None
        elif len(flt) == 1:
            where = dict(flt)
        else:
            where = {"$and": [{k: v} for k, v in flt.items()]}
        res = col.query(
            query_embeddings=[list(vector)], n_results=top_k, where=where, include=["metadatas", "distances"]
        )
        hits = []
        for doc_id, dist, meta in zip(res["ids"][0], res["distances"][0], res["metadatas"][0], strict=False):
            hits.append(
                StoreHit(
                    id=doc_id,
                    score=1.0 - float(dist),
                    document=json.loads(meta["document"]),
                    embedding_model=meta.get("embedding_model"),
                )
            )
        return hits

    def count(self) -> int:
        col = self._get()
        return int(col.count()) if col is not None else 0

    def drop(self) -> None:
        if self._get() is not None:
            self.client.delete_collection(self.name)

    def list_collections(self) -> list[str]:
        return [c.name for c in self.client.list_collections()]

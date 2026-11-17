"""Qdrant adapter. Default backend: embedded local mode (no server), or a Qdrant server / Qdrant Cloud.

Local mode is single-process and not thread-safe, so one client is shared per path and every call is
serialised with a lock.
"""

from __future__ import annotations

import atexit
import re
import threading
import uuid
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from typing import Any

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter

_CLIENTS: dict[str, tuple[Any, threading.Lock]] = {}
_CLIENTS_LOCK = threading.Lock()
_ID_NAMESPACE = uuid.UUID("6b3c1f5e-9a4d-4c1e-8f3a-2a7d9c0e5b11")


def _client(spec: StoreSpec) -> tuple[Any, Any]:
    from qdrant_client import QdrantClient

    if spec.url:
        return QdrantClient(url=spec.url, api_key=spec.api_key, timeout=30), nullcontext()
    if spec.local_path is None:
        raise VectorStoreError("STORE_CONFIG_INVALID", "Local Qdrant needs a storage path")
    key = str(spec.local_path.resolve())
    with _CLIENTS_LOCK:
        if key not in _CLIENTS:
            spec.local_path.mkdir(parents=True, exist_ok=True)
            _CLIENTS[key] = (QdrantClient(path=key), threading.Lock())
        client, lock = _CLIENTS[key]
    return client, lock


def close_local_clients() -> None:
    """Close cached embedded clients (registered at exit; also useful in tests)."""
    with _CLIENTS_LOCK:
        for client, lock in _CLIENTS.values():
            with lock:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - best effort during shutdown
                    pass
        _CLIENTS.clear()


atexit.register(close_local_clients)


def collection_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name)[:255]
    if not clean:
        raise VectorStoreError("STORE_CONFIG_INVALID", "Empty collection name")
    return clean


def point_id(doc_id: str) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, doc_id))


class QdrantStore(VectorStore):
    def __init__(self, spec: StoreSpec) -> None:
        super().__init__(spec)
        self.client, self.lock = _client(spec)
        self.name = collection_name(spec.collection)

    def _distance(self) -> Any:
        from qdrant_client import models

        return models.Distance.COSINE if self.spec.distance == "cosine" else models.Distance.DOT

    def exists(self) -> bool:
        with self.lock:
            return bool(self.client.collection_exists(self.name))

    def ensure_collection(self, dimensions: int) -> None:
        from qdrant_client import models

        with self.lock:
            if self.client.collection_exists(self.name):
                info = self.client.get_collection(self.name)
                params = info.config.params.vectors
                size = getattr(params, "size", None)
                if size is not None and size != dimensions:
                    raise VectorStoreError(
                        "EMBEDDING_DIMENSION_MISMATCH",
                        f"Collection '{self.spec.collection}' stores {size}-d vectors but the model produces "
                        f"{dimensions}-d vectors. Use another collection or re-index with 'recreate'.",
                    )
                return
            self.client.create_collection(
                self.name, vectors_config=models.VectorParams(size=dimensions, distance=self._distance())
            )

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids:
            return {}
        with self.lock:
            records = self.client.retrieve(
                self.name, ids=[point_id(i) for i in ids], with_payload=["doc_id", "content_hash", "embedding_model"]
            )
        return {
            r.payload["doc_id"]: (r.payload.get("content_hash", ""), r.payload.get("embedding_model", ""))
            for r in records
            if r.payload
        }

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        from qdrant_client import models

        points = [
            models.PointStruct(
                id=point_id(r.id),
                vector=r.vector,
                payload={
                    "doc_id": r.id,
                    "document": r.document,
                    "content_hash": r.content_hash,
                    "embedding_model": r.embedding_model,
                    **r.fields,
                },
            )
            for r in records
        ]
        with self.lock:
            self.client.upsert(self.name, points=points, wait=True)

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        from qdrant_client import models

        flt = normalize_filter(filters)
        query_filter = (
            models.Filter(must=[models.FieldCondition(key=k, match=models.MatchValue(value=v)) for k, v in flt.items()])
            if flt
            else None
        )
        with self.lock:
            result = self.client.query_points(
                self.name, query=list(vector), limit=top_k, query_filter=query_filter, with_payload=True
            )
        return [
            StoreHit(
                id=p.payload["doc_id"],
                score=float(p.score),
                document=p.payload["document"],
                embedding_model=p.payload.get("embedding_model"),
            )
            for p in result.points
            if p.payload
        ]

    def count(self) -> int:
        with self.lock:
            return int(self.client.count(self.name, exact=True).count)

    def drop(self) -> None:
        from qdrant_client import models

        with self.lock:
            if self.client.collection_exists(self.name):
                if not self.spec.url:
                    # Embedded mode (seen on Windows): a deleted-then-recreated collection can reload its
                    # old points from local storage, so empty it explicitly before deleting.
                    self.client.delete(
                        self.name, points_selector=models.FilterSelector(filter=models.Filter()), wait=True
                    )
                self.client.delete_collection(self.name)

    def list_collections(self) -> list[str]:
        with self.lock:
            return [c.name for c in self.client.get_collections().collections]

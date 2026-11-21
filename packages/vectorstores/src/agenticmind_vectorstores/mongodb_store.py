"""MongoDB Atlas Vector Search adapter (ADR-0028), with pymongo and a ``dsn`` secret
(``mongodb+srv://user:pass@cluster/...``). ``namespace`` selects the database (default ``agenticmind``).

Each collection gets a ``vectorSearch`` index (``agenticmind_vector``) over ``vector`` with cosine or
dotProduct similarity and the flat filter fields declared as ``filter`` paths, so they pre-filter.
``meta_*`` filters (not declared in the index) are applied with ``$match`` after the search over an
enlarged candidate set. A new index builds asynchronously; ``ensure_collection`` waits until it is
queryable. The dimensions are recorded in ``_agenticmind_collections``. Scores are recomputed from the
returned vectors (exact cosine similarity / dot product).
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .base import (
    FILTER_FIELDS,
    Scalar,
    StoreHit,
    StoreRecord,
    StoreSpec,
    VectorStore,
    VectorStoreError,
    normalize_filter,
    similarity,
)

INDEX = "agenticmind_vector"
META = "_agenticmind_collections"
META_OVERFETCH = 20


def collection_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return clean[:120] or "documents"


def search_pipeline(vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar]) -> list[dict[str, Any]]:
    flat = {k: v for k, v in filters.items() if k in FILTER_FIELDS}
    meta = {k: v for k, v in filters.items() if k not in FILTER_FIELDS}
    limit = top_k * META_OVERFETCH if meta else top_k
    stage: dict[str, Any] = {
        "index": INDEX,
        "path": "vector",
        "queryVector": [float(x) for x in vector],
        "numCandidates": max(100, limit * 20),
        "limit": limit,
    }
    if flat:
        stage["filter"] = {k: {"$eq": v} for k, v in flat.items()}
    pipeline: list[dict[str, Any]] = [{"$vectorSearch": stage}]
    if meta:
        pipeline.append({"$match": meta})
        pipeline.append({"$limit": top_k})
    pipeline.append({"$project": {"document": 1, "embedding_model": 1, "vector": 1,
                                  "score": {"$meta": "vectorSearchScore"}}})  # fmt: skip
    return pipeline


class MongoDBStore(VectorStore):
    def __init__(self, spec: StoreSpec, client: Any = None, *, wait_seconds: float = 120) -> None:
        super().__init__(spec)
        if client is None:
            if not spec.dsn:
                raise VectorStoreError("STORE_CONFIG_INVALID", "MongoDB needs a connection string (dsn secret)")
            try:
                from pymongo import MongoClient
            except ImportError as exc:  # pragma: no cover - optional extra
                raise VectorStoreError(
                    "STORE_NOT_INSTALLED", "MongoDB support needs 'pymongo' (agenticmind-vectorstores[mongodb])"
                ) from exc
            client = MongoClient(spec.dsn, serverSelectionTimeoutMS=10_000, appname="AgenticMind")
        self.client = client
        self.db = client[spec.namespace or "agenticmind"]
        self.name = collection_name(spec.collection)
        self.wait_seconds = wait_seconds

    @property
    def coll(self) -> Any:
        return self.db[self.name]

    def _guard(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except VectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - pymongo errors
            name = type(exc).__name__
            if name in ("ServerSelectionTimeoutError", "AutoReconnect", "NetworkTimeout", "ConnectionFailure"):
                raise VectorStoreError("STORE_UNAVAILABLE", f"MongoDB: {exc}", retryable=True) from exc
            if name == "OperationFailure" and "auth" in str(exc).lower():
                raise VectorStoreError("STORE_AUTH_FAILED", "MongoDB rejected the credentials") from exc
            raise VectorStoreError("STORE_REQUEST_FAILED", f"MongoDB: {exc}") from exc

    def exists(self) -> bool:
        return self._guard(lambda: self.db[META].find_one({"_id": self.name}) is not None)

    def ensure_collection(self, dimensions: int) -> None:
        meta = self._guard(self.db[META].find_one, {"_id": self.name})
        if meta is not None:
            if int(meta["dimensions"]) != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Collection '{self.name}' stores {meta['dimensions']}-d vectors but the model produces "
                    f"{dimensions}-d.",
                )
            return
        from pymongo.operations import SearchIndexModel

        if self.name not in self._guard(self.db.list_collection_names):
            self._guard(self.db.create_collection, self.name)
        similarity_name = "cosine" if self.spec.distance == "cosine" else "dotProduct"
        fields = [{"type": "vector", "path": "vector", "numDimensions": dimensions, "similarity": similarity_name}]
        fields += [{"type": "filter", "path": f} for f in FILTER_FIELDS]
        model = SearchIndexModel(definition={"fields": fields}, name=INDEX, type="vectorSearch")
        self._guard(self.coll.create_search_index, model=model)
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            indexes = list(self._guard(self.coll.list_search_indexes, INDEX))
            if indexes and indexes[0].get("queryable"):
                break
            time.sleep(2)
        else:
            raise VectorStoreError(
                "STORE_UNAVAILABLE", f"The Atlas vector index on '{self.name}' is still building", retryable=True
            )
        self._guard(self.db[META].insert_one, {"_id": self.name, "dimensions": dimensions,
                                               "distance": self.spec.distance})  # fmt: skip

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids:
            return {}
        cursor = self._guard(self.coll.find, {"_id": {"$in": list(ids)}}, {"content_hash": 1, "embedding_model": 1})
        return {d["_id"]: (str(d.get("content_hash", "")), str(d.get("embedding_model", ""))) for d in cursor}

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        from pymongo import ReplaceOne

        if not records:
            return
        ops = [
            ReplaceOne(
                {"_id": r.id},
                {
                    "_id": r.id,
                    "vector": [float(x) for x in r.vector],
                    "document": r.document,
                    "content_hash": r.content_hash,
                    "embedding_model": r.embedding_model,
                    **r.fields,
                },
                upsert=True,
            )  # fmt: skip
            for r in records
        ]
        self._guard(self.coll.bulk_write, ops, ordered=False)

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"MongoDB collection '{self.name}' is not indexed yet")
        flt = normalize_filter(filters)
        rows = self._guard(lambda: list(self.coll.aggregate(search_pipeline(vector, top_k, flt))))
        hits = [
            StoreHit(
                id=str(r["_id"]),
                score=similarity(vector, r.get("vector") or [], self.spec.distance),
                document=r["document"],
                embedding_model=r.get("embedding_model"),
            )  # fmt: skip
            for r in rows
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def count(self) -> int:
        return int(self._guard(self.coll.count_documents, {})) if self.exists() else 0

    def drop(self) -> None:
        self._guard(self.coll.drop)
        self._guard(self.db[META].delete_one, {"_id": self.name})

"""Weaviate adapter (ADR-0028), Python client v4: a server (``url`` http(s)://host:8080, gRPC on 50051)
or Weaviate Cloud (``url`` of the cluster + ``api_key``).

Collections are created with self-provided vectors (``Configure.Vectors.self_provided``, the v4.16+ API)
and an HNSW index using cosine or dot distance; the embedding dimensions are kept in the collection
description so mismatches are caught before any vector is written. Object UUIDs are derived from the record
ids (uuid5), so upserts overwrite. Properties beyond the declared ones (``meta_*``) are added by Weaviate's
auto-schema. Scores are recomputed from the returned vectors (exact cosine similarity / dot product).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter, similarity

_NS = uuid.UUID("6f1b0c4e-8a63-4c55-9a39-2f8a1d7c5e10")
_DIMS = re.compile(r"agenticmind dims=(\d+)")


def class_name(name: str) -> str:
    """Weaviate collection names start with an upper-case letter; letters, digits and underscores only."""
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not clean[:1].isalpha():
        clean = f"C{clean}"
    return clean[0].upper() + clean[1:]


def object_uuid(record_id: str) -> str:
    return str(uuid.uuid5(_NS, record_id))


def connect(spec: StoreSpec) -> Any:
    import weaviate
    from weaviate.classes.init import Auth

    if not spec.url:
        raise VectorStoreError("STORE_CONFIG_INVALID", "Weaviate needs a URL (server or Weaviate Cloud)")
    auth = Auth.api_key(spec.api_key) if spec.api_key else None
    parsed = urlparse(spec.url)
    host = parsed.hostname or ""
    try:
        if host.endswith((".weaviate.cloud", ".weaviate.network", ".wcs.api.weaviate.io")):
            return weaviate.connect_to_weaviate_cloud(cluster_url=spec.url, auth_credentials=auth)
        secure = parsed.scheme == "https"
        return weaviate.connect_to_custom(
            http_host=host, http_port=parsed.port or (443 if secure else 8080), http_secure=secure,
            grpc_host=host, grpc_port=50051, grpc_secure=secure, auth_credentials=auth,
        )  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - the client raises several connection error types
        raise VectorStoreError("STORE_UNAVAILABLE", f"Cannot connect to Weaviate: {exc}", retryable=True) from exc


def _vector(obj: Any) -> list[float]:
    vec = getattr(obj, "vector", None)
    if isinstance(vec, Mapping):
        vec = vec.get("default") or next(iter(vec.values()), None)
    return list(vec or [])


class WeaviateStore(VectorStore):
    def __init__(self, spec: StoreSpec, client: Any = None) -> None:
        super().__init__(spec)
        if client is None:
            try:
                import weaviate  # noqa: F401
            except ImportError as exc:  # pragma: no cover - optional extra
                raise VectorStoreError(
                    "STORE_NOT_INSTALLED",
                    "Weaviate support needs 'weaviate-client' (agenticmind-vectorstores[weaviate])",
                ) from exc
            client = connect(spec)
        self.client = client
        self.name = class_name(spec.collection)

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    def _coll(self) -> Any:
        collections = self.client.collections
        return collections.use(self.name) if hasattr(collections, "use") else collections.get(self.name)

    def exists(self) -> bool:
        return bool(self.client.collections.exists(self.name))

    def ensure_collection(self, dimensions: int) -> None:
        from weaviate.classes.config import Configure, DataType, Property, VectorDistances

        if self.exists():
            match = _DIMS.search(self._coll().config.get().description or "")
            if match and int(match.group(1)) != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Collection '{self.name}' stores {match.group(1)}-d vectors but the model produces "
                    f"{dimensions}-d.",
                )
            return
        metric = VectorDistances.COSINE if self.spec.distance == "cosine" else VectorDistances.DOT
        text = DataType.TEXT
        self.client.collections.create(
            self.name,
            description=f"AgenticMind collection (agenticmind dims={dimensions})",
            vector_config=Configure.Vectors.self_provided(
                vector_index_config=Configure.VectorIndex.hnsw(distance_metric=metric)
            ),
            properties=[
                Property(name="record_id", data_type=text),
                Property(name="document", data_type=text, index_searchable=False, index_filterable=False),
                Property(name="content_hash", data_type=text),
                Property(name="embedding_model", data_type=text),
                Property(name="filename", data_type=text),
                Property(name="artifact_id", data_type=text),
                Property(name="page", data_type=DataType.INT),
                Property(name="section", data_type=text),
                Property(name="parent_id", data_type=text),
            ],
        )

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids or not self.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        coll = self._coll()
        for start in range(0, len(ids), 200):
            resp = coll.query.fetch_objects_by_ids(ids=[object_uuid(i) for i in ids[start : start + 200]])
            for obj in resp.objects:
                p = obj.properties
                out[str(p["record_id"])] = (str(p.get("content_hash") or ""), str(p.get("embedding_model") or ""))
        return out

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        from weaviate.classes.data import DataObject

        if not records:
            return
        objects = [
            DataObject(
                uuid=object_uuid(r.id),
                vector=[float(x) for x in r.vector],
                properties={
                    "record_id": r.id,
                    "document": json.dumps(r.document, ensure_ascii=False),
                    "content_hash": r.content_hash,
                    "embedding_model": r.embedding_model,
                    **r.fields,
                },
            )
            for r in records
        ]
        result = self._coll().data.insert_many(objects)
        if getattr(result, "has_errors", False):
            first = next(iter((getattr(result, "errors", None) or {}).values()), "unknown error")
            raise VectorStoreError("STORE_WRITE_FAILED", f"Weaviate rejected objects: {first}")

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        from weaviate.classes.query import Filter, MetadataQuery

        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Weaviate collection '{self.name}' does not exist")
        flt = normalize_filter(filters)
        where = None
        for key, value in flt.items():
            cond = Filter.by_property(key).equal(value)
            where = cond if where is None else where & cond
        resp = self._coll().query.near_vector(
            near_vector=[float(x) for x in vector], limit=top_k, filters=where,
            return_metadata=MetadataQuery(distance=True), include_vector=True,
        )  # fmt: skip
        hits = []
        for obj in resp.objects:
            p = obj.properties
            hits.append(
                StoreHit(
                    id=str(p["record_id"]),
                    score=similarity(vector, _vector(obj), self.spec.distance),
                    document=json.loads(p["document"]),
                    embedding_model=p.get("embedding_model"),
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    def count(self) -> int:
        if not self.exists():
            return 0
        return int(self._coll().aggregate.over_all(total_count=True).total_count or 0)

    def drop(self) -> None:
        if self.exists():
            self.client.collections.delete(self.name)

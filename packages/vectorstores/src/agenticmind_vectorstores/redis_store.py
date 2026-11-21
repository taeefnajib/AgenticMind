"""Redis adapter (ADR-0028): Redis 8 / Redis Stack query engine (RediSearch), via redis-py.

``url`` (``redis://[:password@]host:6379/0`` or ``rediss://`` for TLS) or a ``dsn`` secret holding the URL.
Records are hashes under ``am:<collection>:<id>`` with the vector as FLOAT32 bytes; the index
``am_idx:<collection>`` has an HNSW vector field (COSINE or IP) plus TAG/NUMERIC fields for filtering.
New ``meta_*`` fields are added to the index with ``FT.ALTER`` the first time they appear, so every filter
runs inside the KNN query. Redis returns a distance (COSINE: 1 - cosine, IP: 1 - dot), turned back into the
contract's similarity. Dimensions and indexed fields are kept in ``am_meta:<collection>``.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter

BASE_TAGS = ("content_hash", "embedding_model", "filename", "artifact_id", "section", "parent_id")
BASE_NUMERIC = ("page",)


def escape_tag(value: str) -> str:
    """Escape TAG query values (every non-alphanumeric character, per RediSearch syntax)."""
    return re.sub(r"([^A-Za-z0-9_])", r"\\\1", value)


def filter_query(filters: Mapping[str, Scalar], numeric: set[str]) -> str:
    clauses = []
    for key, value in filters.items():
        if key in numeric and not isinstance(value, bool):
            clauses.append(f"@{key}:[{value} {value}]")
        else:
            text = ("true" if value else "false") if isinstance(value, bool) else str(value)
            clauses.append(f"@{key}:{{{escape_tag(text)}}}")
    return " ".join(clauses)


def pack(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *(float(x) for x in vector))


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisStore(VectorStore):
    def __init__(self, spec: StoreSpec, client: Any = None) -> None:
        super().__init__(spec)
        if client is None:
            url = spec.dsn or spec.url
            if not url:
                raise VectorStoreError("STORE_CONFIG_INVALID", "Redis needs a URL (redis://host:6379) or a dsn secret")
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - optional extra
                raise VectorStoreError(
                    "STORE_NOT_INSTALLED", "Redis support needs 'redis' (agenticmind-vectorstores[redis])"
                ) from exc
            client = redis.Redis.from_url(url, socket_timeout=30, socket_connect_timeout=10)
        self.r = client
        self.name = re.sub(r"[^A-Za-z0-9_-]", "_", spec.collection)
        self.index = f"am_idx:{self.name}"
        self.prefix = f"am:{self.name}:"
        self.meta_key = f"am_meta:{self.name}"

    def _guard(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except VectorStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - redis-py errors
            name = type(exc).__name__
            if name in ("ConnectionError", "TimeoutError", "BusyLoadingError"):
                raise VectorStoreError("STORE_UNAVAILABLE", f"Redis: {exc}", retryable=True) from exc
            if name == "AuthenticationError":
                raise VectorStoreError("STORE_AUTH_FAILED", "Redis rejected the credentials") from exc
            if "unknown command" in str(exc).lower() and "ft." in str(exc).lower():
                raise VectorStoreError(
                    "STORE_UNSUPPORTED", "This Redis has no query engine (use Redis 8 or Redis Stack)"
                ) from exc
            raise VectorStoreError("STORE_REQUEST_FAILED", f"Redis: {exc}") from exc

    def _meta(self) -> dict[str, Any] | None:
        raw = self._guard(self.r.hgetall, self.meta_key)
        if not raw:
            return None
        meta = {_text(k): _text(v) for k, v in raw.items()}
        return {"dimensions": int(meta["dimensions"]), "fields": json.loads(meta.get("fields", "{}"))}

    def exists(self) -> bool:
        return self._meta() is not None

    def ensure_collection(self, dimensions: int) -> None:
        from redis.commands.search.field import NumericField, TagField, VectorField

        try:
            from redis.commands.search.index_definition import IndexDefinition, IndexType
        except ImportError:  # pragma: no cover - redis-py < 6
            from redis.commands.search.indexDefinition import IndexDefinition, IndexType  # type: ignore

        meta = self._meta()
        if meta is not None:
            if meta["dimensions"] != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Collection '{self.name}' stores {meta['dimensions']}-d vectors but the model produces "
                    f"{dimensions}-d.",
                )
            return
        schema = [TagField(f) for f in BASE_TAGS] + [NumericField(f) for f in BASE_NUMERIC]
        metric = "COSINE" if self.spec.distance == "cosine" else "IP"
        schema.append(VectorField("vector", "HNSW", {"TYPE": "FLOAT32", "DIM": dimensions, "DISTANCE_METRIC": metric}))
        self._guard(self.r.ft(self.index).create_index, schema,
                    definition=IndexDefinition(prefix=[self.prefix], index_type=IndexType.HASH))  # fmt: skip
        fields = {f: "tag" for f in BASE_TAGS} | {f: "numeric" for f in BASE_NUMERIC}
        self._guard(self.r.hset, self.meta_key, mapping={"dimensions": dimensions, "fields": json.dumps(fields)})

    def _index_new_fields(self, records: Sequence[StoreRecord]) -> None:
        from redis.commands.search.field import NumericField, TagField

        meta = self._meta() or {"fields": {}}
        known: dict[str, str] = meta["fields"]
        added = {}
        for r in records:
            for key, value in r.fields.items():
                if key not in known and key not in added:
                    added[key] = "numeric" if isinstance(value, (int, float)) and not isinstance(value, bool) else "tag"
        for key, kind in added.items():
            field = NumericField(key) if kind == "numeric" else TagField(key)
            self._guard(self.r.ft(self.index).alter_schema_add, [field])
        if added:
            self._guard(self.r.hset, self.meta_key, "fields", json.dumps(known | added))

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids:
            return {}
        pipe = self.r.pipeline(transaction=False)
        for i in ids:
            pipe.hmget(self.prefix + i, ["content_hash", "embedding_model"])
        rows = self._guard(pipe.execute)
        return {i: (_text(h), _text(m)) for i, (h, m) in zip(ids, rows, strict=True) if h is not None}

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        if not records:
            return
        self._index_new_fields(records)
        pipe = self.r.pipeline(transaction=False)
        for r in records:
            fields = {k: (("true" if v else "false") if isinstance(v, bool) else v) for k, v in r.fields.items()}
            pipe.delete(self.prefix + r.id)  # drop fields of a previous version
            pipe.hset(self.prefix + r.id, mapping={
                "vector": pack(r.vector), "document": json.dumps(r.document, ensure_ascii=False),
                "content_hash": r.content_hash, "embedding_model": r.embedding_model, **fields,
            })  # fmt: skip
        self._guard(pipe.execute)

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        from redis.commands.search.query import Query

        meta = self._meta()
        if meta is None:
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Redis collection '{self.name}' does not exist")
        flt = normalize_filter(filters)
        unknown = [k for k in flt if k not in meta["fields"]]
        if unknown:  # a field no record has: nothing can match
            return []
        numeric = {k for k, kind in meta["fields"].items() if kind == "numeric"}
        base = filter_query(flt, numeric)
        text = (
            f"({base})=>[KNN {top_k} @vector $vec AS __score]" if base else f"*=>[KNN {top_k} @vector $vec AS __score]"
        )
        q = (
            Query(text).sort_by("__score").return_fields("document", "embedding_model", "__score")
            .paging(0, top_k).dialect(2)
        )  # fmt: skip
        result = self._guard(self.r.ft(self.index).search, q, query_params={"vec": pack(vector)})
        hits = []
        for doc in result.docs:
            hits.append(
                StoreHit(
                    id=_text(doc.id)[len(self.prefix) :],
                    score=1.0 - float(_text(doc["__score"])),  # COSINE: 1 - cos; IP: 1 - dot
                    document=json.loads(_text(doc["document"])),
                    embedding_model=_text(doc["embedding_model"]) if getattr(doc, "embedding_model", None) else None,
                )
            )
        return hits

    def count(self) -> int:
        if not self.exists():
            return 0
        info = self._guard(self.r.ft(self.index).info)
        return int(_text(info.get("num_docs", 0)))

    def drop(self) -> None:
        if self.exists():
            try:
                self.r.ft(self.index).dropindex(delete_documents=True)
            except Exception:  # noqa: BLE001 - index already gone
                pass
            self._guard(self.r.delete, self.meta_key)

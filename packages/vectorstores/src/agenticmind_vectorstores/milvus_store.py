"""Milvus adapter (ADR-0028): a Milvus server (``url`` like http://localhost:19530, ``api_key`` = token
"user:password"), Zilliz Cloud (endpoint + API key), or Milvus Lite when no URL is set (a local file;
Milvus Lite is published for Linux and macOS only).

Collections use string primary keys, a ``vector`` field with COSINE or IP metric, and dynamic fields for the
document JSON, hashes and filterable fields, so ``meta_*`` filters work without a schema change. For both
metrics Milvus returns a similarity (higher = closer), which is used as the score. Reads use
``consistency_level="Strong"`` so freshly upserted vectors are visible.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter

OUTPUT = ["document", "content_hash", "embedding_model"]


def collection_name(name: str) -> str:
    """Milvus names: letters, digits and underscores, starting with a letter or underscore (max 255)."""
    clean = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not re.match(r"[A-Za-z_]", clean):
        clean = f"c_{clean}"
    return clean[:255]


def filter_expr(filters: Mapping[str, Scalar]) -> str:
    parts = []
    for key, value in filters.items():
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, (int, float)):
            literal = repr(value)
        else:
            literal = json.dumps(str(value))  # double-quoted, escaped
        parts.append(f"{key} == {literal}")
    return " and ".join(parts)


class MilvusStore(VectorStore):
    def __init__(self, spec: StoreSpec, client: Any = None) -> None:
        super().__init__(spec)
        self.name = collection_name(spec.collection)
        if client is not None:
            self.client = client
            return
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:  # pragma: no cover - optional extra
            raise VectorStoreError(
                "STORE_NOT_INSTALLED", "Milvus support needs 'pymilvus' (agenticmind-vectorstores[milvus])"
            ) from exc
        if spec.url:
            uri = spec.url
        elif spec.local_path is not None:
            spec.local_path.mkdir(parents=True, exist_ok=True)
            uri = str(spec.local_path / "milvus.db")
        else:
            raise VectorStoreError("STORE_CONFIG_INVALID", "Milvus needs a URL (or a local path for Milvus Lite)")
        try:
            self.client = MilvusClient(uri=uri, token=spec.api_key or "")
        except Exception as exc:  # noqa: BLE001 - connection errors come in several types
            raise VectorStoreError("STORE_UNAVAILABLE", f"Cannot connect to Milvus: {exc}", retryable=True) from exc

    def exists(self) -> bool:
        return bool(self.client.has_collection(collection_name=self.name))

    def _dims(self) -> int | None:
        info = self.client.describe_collection(collection_name=self.name)
        for f in info.get("fields", []):
            params = f.get("params") or {}
            if f.get("name") == "vector" and "dim" in params:
                return int(params["dim"])
        return None

    def ensure_collection(self, dimensions: int) -> None:
        if self.exists():
            dims = self._dims()
            if dims is not None and dims != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Collection '{self.name}' stores {dims}-d vectors but the model produces {dimensions}-d.",
                )
            return
        self.client.create_collection(
            collection_name=self.name,
            dimension=dimensions,
            primary_field_name="id",
            id_type="string",
            max_length=512,
            vector_field_name="vector",
            metric_type="COSINE" if self.spec.distance == "cosine" else "IP",
            auto_id=False,
            enable_dynamic_field=True,
            consistency_level="Strong",
        )

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids or not self.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        for start in range(0, len(ids), 500):
            rows = self.client.get(collection_name=self.name, ids=list(ids[start : start + 500]),
                                   output_fields=["content_hash", "embedding_model"])  # fmt: skip
            for row in rows:
                out[str(row["id"])] = (str(row.get("content_hash", "")), str(row.get("embedding_model", "")))
        return out

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        if not records:
            return
        data = [
            {
                "id": r.id,
                "vector": [float(x) for x in r.vector],
                "document": json.dumps(r.document, ensure_ascii=False),
                "content_hash": r.content_hash,
                "embedding_model": r.embedding_model,
                **r.fields,
            }
            for r in records
        ]
        self.client.upsert(collection_name=self.name, data=data)

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Milvus collection '{self.name}' does not exist")
        flt = normalize_filter(filters)
        kwargs: dict[str, Any] = {}
        if flt:
            kwargs["filter"] = filter_expr(flt)
        results = self.client.search(
            collection_name=self.name,
            data=[[float(x) for x in vector]],
            limit=top_k,
            output_fields=OUTPUT,
            search_params={"metric_type": "COSINE" if self.spec.distance == "cosine" else "IP", "params": {}},
            consistency_level="Strong",
            **kwargs,
        )
        hits = []
        for hit in results[0] if results else []:
            entity = hit.get("entity") or {}
            hits.append(
                StoreHit(
                    id=str(hit["id"]),
                    score=float(hit["distance"]),  # COSINE / IP: a similarity, higher is closer
                    document=json.loads(entity["document"]),
                    embedding_model=entity.get("embedding_model"),
                )
            )
        return hits

    def count(self) -> int:
        if not self.exists():
            return 0
        rows = self.client.query(collection_name=self.name, filter="", output_fields=["count(*)"],
                                 consistency_level="Strong")  # fmt: skip
        return int(rows[0]["count(*)"]) if rows else 0

    def drop(self) -> None:
        if self.exists():
            self.client.drop_collection(collection_name=self.name)

    def list_collections(self) -> list[str]:
        return list(self.client.list_collections())

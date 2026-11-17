"""Pinecone adapter (serverless dense index with dimension/metric; ``api_key`` secret).

Index names allow lowercase letters, digits and '-' (max 45 chars). Documents are stored as a JSON
string in metadata (Pinecone limits metadata to 40 KB per vector). ``namespace`` maps to a Pinecone
namespace. Written against pinecone>=10 (``create_index``/``has_index``/``Index.upsert``/``query``/``fetch``).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter

MAX_METADATA_BYTES = 38_000


def index_name(name: str) -> str:
    clean = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    clean = re.sub(r"-{2,}", "-", clean)[:45].strip("-")
    if not clean:
        raise VectorStoreError("STORE_CONFIG_INVALID", "Empty Pinecone index name")
    return clean


class PineconeStore(VectorStore):
    def __init__(self, spec: StoreSpec) -> None:
        super().__init__(spec)
        if not spec.api_key:
            raise VectorStoreError("STORE_CONFIG_INVALID", "Pinecone needs an API key (api_key secret)")
        try:
            from pinecone import Pinecone
        except ImportError as exc:  # pragma: no cover - optional extra
            raise VectorStoreError(
                "STORE_NOT_INSTALLED", "Pinecone support needs 'pinecone' (agenticmind-vectorstores[pinecone])"
            ) from exc
        self.pc = Pinecone(api_key=spec.api_key)
        self.name = index_name(spec.collection)
        self.namespace = spec.namespace or ""
        self._index: Any = None

    def _idx(self) -> Any:
        if self._index is None:
            self._index = self.pc.Index(name=self.name)
        return self._index

    def exists(self) -> bool:
        return bool(self.pc.has_index(self.name))

    def ensure_collection(self, dimensions: int) -> None:
        from pinecone import ServerlessSpec

        if self.exists():
            desc = self.pc.describe_index(self.name)
            if getattr(desc, "dimension", None) not in (None, dimensions):
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Index '{self.name}' stores {desc.dimension}-d vectors but the model produces {dimensions}-d.",
                )
            return
        self.pc.create_index(
            name=self.name,
            dimension=dimensions,
            metric="cosine" if self.spec.distance == "cosine" else "dotproduct",
            spec=ServerlessSpec(cloud=self.spec.cloud or "aws", region=self.spec.region or "us-east-1"),
        )
        deadline = time.time() + 120
        while time.time() < deadline:
            status = getattr(self.pc.describe_index(self.name), "status", None)
            ready = getattr(status, "ready", None) if status is not None else None
            if ready is None and isinstance(status, Mapping):
                ready = status.get("ready")
            if ready:
                return
            time.sleep(2)
        raise VectorStoreError(
            "STORE_UNAVAILABLE", f"Pinecone index '{self.name}' did not become ready", retryable=True
        )

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids or not self.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        for start in range(0, len(ids), 100):
            resp = self._idx().fetch(ids=list(ids[start : start + 100]), namespace=self.namespace)
            for vid, vec in (resp.vectors or {}).items():
                meta = getattr(vec, "metadata", None) or {}
                out[vid] = (str(meta.get("content_hash", "")), str(meta.get("embedding_model", "")))
        return out

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        vectors = []
        for r in records:
            doc_json = json.dumps(r.document, ensure_ascii=False)
            if len(doc_json.encode("utf-8")) > MAX_METADATA_BYTES:
                raise VectorStoreError(
                    "DOCUMENT_TOO_LARGE", f"Chunk '{r.id}' exceeds Pinecone's metadata limit; use smaller chunks"
                )
            vectors.append(
                {
                    "id": r.id,
                    "values": r.vector,
                    "metadata": {
                        "document": doc_json,
                        "content_hash": r.content_hash,
                        "embedding_model": r.embedding_model,
                        **r.fields,
                    },
                }
            )
        self._idx().upsert(vectors=vectors, namespace=self.namespace, batch_size=100, show_progress=False)

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Pinecone index '{self.name}' does not exist")
        flt = normalize_filter(filters)
        resp = self._idx().query(
            top_k=top_k,
            vector=list(vector),
            namespace=self.namespace,
            filter={k: {"$eq": v} for k, v in flt.items()} or None,
            include_metadata=True,
        )
        hits = []
        for match in resp.matches or []:
            meta = match.metadata or {}
            hits.append(
                StoreHit(
                    id=match.id,
                    score=float(match.score),
                    document=json.loads(meta["document"]),
                    embedding_model=meta.get("embedding_model"),
                )
            )
        return hits

    def count(self) -> int:
        if not self.exists():
            return 0
        stats = self._idx().describe_index_stats()
        namespaces = getattr(stats, "namespaces", None) or {}
        ns = namespaces.get(self.namespace) if isinstance(namespaces, Mapping) else None
        if ns is not None:
            return int(getattr(ns, "vector_count", 0) or 0)
        return int(getattr(stats, "total_vector_count", 0) or 0)

    def drop(self) -> None:
        if self.exists():
            self.pc.delete_index(self.name)
            self._index = None

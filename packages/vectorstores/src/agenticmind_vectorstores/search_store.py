"""Elasticsearch and OpenSearch adapters (ADR-0028), over their REST APIs with httpx (no client library).

* **Elasticsearch 8/9**: ``dense_vector`` (``similarity`` cosine or ``max_inner_product``, plain ``hnsw``
  index options so vectors are not quantised) and the top-level ``knn`` search with a ``filter``.
  ``api_key`` is sent as ``Authorization: ApiKey <key>``; user:password in the URL works too.
* **OpenSearch 2/3**: ``index.knn: true`` and ``knn_vector`` with the Lucene engine (``cosinesimil`` or
  ``innerproduct``), queried with a ``knn`` query whose ``filter`` is applied during the search. Use
  user:password in the URL (Amazon OpenSearch Service SigV4 signing is not supported).

The document is stored unindexed; flat fields are ``keyword``/``long`` (``meta_*`` strings become keywords
through a dynamic template). Writes use ``refresh=wait_for`` so they are searchable when the call returns.
Engine scores are rescaled differently per product and similarity, so the score is recomputed from the
returned vector: an exact cosine similarity / dot product, the same as on every other backend.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, urlparse

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter, similarity

KEYWORD_FIELDS = ("content_hash", "embedding_model", "filename", "artifact_id", "section", "parent_id")


def index_name(name: str) -> str:
    """Lower-case; no characters Elasticsearch/OpenSearch forbid; cannot start with -, _ or +."""
    clean = re.sub(r"[^a-z0-9_-]", "-", name.lower()).lstrip("-_+")
    if not clean:
        raise VectorStoreError("STORE_CONFIG_INVALID", "Empty index name")
    return clean[:255]


class SearchEngineStore(VectorStore):
    def __init__(self, spec: StoreSpec, transport: Any = None) -> None:
        super().__init__(spec)
        import httpx

        if not spec.url:
            raise VectorStoreError("STORE_CONFIG_INVALID", f"{spec.backend} needs a URL, e.g. https://localhost:9200")
        self.opensearch = spec.backend == "opensearch"
        parsed = urlparse(spec.url)
        auth = None
        uses_api_key = bool(spec.api_key) and not self.opensearch
        if parsed.username and not uses_api_key:  # an explicit API key wins over URL credentials
            auth = (unquote(parsed.username), unquote(parsed.password or ""))
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc += f":{parsed.port}"
        self.base = f"{parsed.scheme}://{netloc}{parsed.path.rstrip('/')}"
        headers = {"Content-Type": "application/json"}
        if spec.api_key and not self.opensearch:  # OpenSearch: credentials go in the URL (user:password@host)
            headers["Authorization"] = f"ApiKey {spec.api_key}"
        self.http = httpx.Client(headers=headers, auth=auth, timeout=httpx.Timeout(60, connect=10), transport=transport)
        self.name = index_name(spec.collection)

    def __del__(self) -> None:  # pragma: no cover - best effort
        try:
            self.http.close()
        except Exception:  # noqa: BLE001
            pass

    # -- HTTP -------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, *, json_body: Any = None, content: str | None = None,
              params: Mapping[str, str] | None = None, ok: tuple[int, ...] = (200, 201)) -> Any:  # fmt: skip
        import httpx

        try:
            resp = self.http.request(
                method, f"{self.base}/{path}", json=json_body, content=content, params=params,
                headers={"Content-Type": "application/x-ndjson"} if content is not None else None,
            )  # fmt: skip
        except httpx.HTTPError as exc:
            raise VectorStoreError("STORE_UNAVAILABLE", f"{self.spec.backend}: {exc}", retryable=True) from exc
        if resp.status_code in ok:
            return resp.json() if resp.content and method != "HEAD" else None
        if resp.status_code in (401, 403):
            raise VectorStoreError("STORE_AUTH_FAILED", f"{self.spec.backend} rejected the credentials")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise VectorStoreError("STORE_UNAVAILABLE", f"{self.spec.backend} returned {resp.status_code}",
                                   retryable=True)  # fmt: skip
        raise VectorStoreError("STORE_REQUEST_FAILED", f"{self.spec.backend} {resp.status_code}: {resp.text[:300]}")

    # -- contract ---------------------------------------------------------------------------------
    def exists(self) -> bool:
        import httpx

        try:
            resp = self.http.head(f"{self.base}/{self.name}")
        except httpx.HTTPError as exc:
            raise VectorStoreError("STORE_UNAVAILABLE", f"{self.spec.backend}: {exc}", retryable=True) from exc
        return resp.status_code == 200

    def _dims(self) -> int | None:
        mapping = self._call("GET", f"{self.name}/_mapping")
        props = next(iter(mapping.values()), {}).get("mappings", {}).get("properties", {})
        vec = props.get("vector", {})
        dims = vec.get("dimension" if self.opensearch else "dims")
        return int(dims) if dims is not None else None

    def ensure_collection(self, dimensions: int) -> None:
        if self.exists():
            dims = self._dims()
            if dims is not None and dims != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Index '{self.name}' stores {dims}-d vectors but the model produces {dimensions}-d.",
                )
            return
        cosine = self.spec.distance == "cosine"
        if self.opensearch:
            vector = {
                "type": "knn_vector",
                "dimension": dimensions,
                "method": {
                    "name": "hnsw",
                    "engine": "lucene",
                    "space_type": "cosinesimil" if cosine else "innerproduct",
                },
            }
            settings: dict[str, Any] = {"index": {"knn": True}}
        else:
            vector = {
                "type": "dense_vector",
                "dims": dimensions,
                "index": True,
                "similarity": "cosine" if cosine else "max_inner_product",
                "index_options": {"type": "hnsw"},
            }
            settings = {}
        # fmt: skip
        properties = {
            "vector": vector,
            "document": {"type": "object", "enabled": False},
            "page": {"type": "long"},
            **{f: {"type": "keyword"} for f in KEYWORD_FIELDS},
        }
        body: dict[str, Any] = {
            "mappings": {
                "dynamic_templates": [
                    {
                        "meta_strings": {
                            "match": "meta_*",
                            "match_mapping_type": "string",
                            "mapping": {"type": "keyword"},
                        }
                    }
                ],
                "properties": properties,
            }
        }
        if settings:
            body["settings"] = settings
        self._call("PUT", self.name, json_body=body)

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids or not self.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        for start in range(0, len(ids), 500):
            resp = self._call("POST", f"{self.name}/_mget", json_body={"ids": list(ids[start : start + 500])},
                              params={"_source_includes": "content_hash,embedding_model"})  # fmt: skip
            for doc in resp.get("docs", []):
                if doc.get("found"):
                    src = doc.get("_source") or {}
                    out[doc["_id"]] = (str(src.get("content_hash", "")), str(src.get("embedding_model", "")))
        return out

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        if not records:
            return
        lines = []
        for r in records:
            lines.append(json.dumps({"index": {"_index": self.name, "_id": r.id}}))
            lines.append(json.dumps({
                "vector": [float(x) for x in r.vector], "document": r.document, "content_hash": r.content_hash,
                "embedding_model": r.embedding_model, **r.fields,
            }, ensure_ascii=False))  # fmt: skip
        resp = self._call("POST", "_bulk", content="\n".join(lines) + "\n", params={"refresh": "wait_for"})
        if resp and resp.get("errors"):
            failed = next((i for i in resp.get("items", []) if next(iter(i.values())).get("error")), None)
            reason = next(iter(failed.values()))["error"] if failed else "unknown"
            raise VectorStoreError("STORE_WRITE_FAILED", f"{self.spec.backend} rejected documents: {reason}")

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Index '{self.name}' does not exist")
        flt = normalize_filter(filters)
        terms = [{"term": {k: v}} for k, v in flt.items()]
        q = [float(x) for x in vector]
        source = ["document", "embedding_model", "vector"]
        if self.opensearch:
            knn: dict[str, Any] = {"vector": q, "k": top_k}
            if terms:
                knn["filter"] = {"bool": {"filter": terms}}
            body: dict[str, Any] = {"size": top_k, "query": {"knn": {"vector": knn}}, "_source": source}
        else:
            knn = {"field": "vector", "query_vector": q, "k": top_k, "num_candidates": max(100, top_k * 10)}
            if terms:
                knn["filter"] = {"bool": {"filter": terms}}
            body = {"size": top_k, "knn": knn, "_source": source}
        resp = self._call("POST", f"{self.name}/_search", json_body=body)
        hits = []
        for h in resp.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            stored = src.get("vector")
            if stored:
                score = similarity(q, stored, self.spec.distance)
            else:  # vectors excluded from _source: undo the cosine rescaling (1 + cos) / 2
                score = 2 * float(h.get("_score") or 0) - 1
            hits.append(StoreHit(id=h["_id"], score=score, document=src["document"],
                                 embedding_model=src.get("embedding_model")))  # fmt: skip
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    def count(self) -> int:
        if not self.exists():
            return 0
        return int(self._call("GET", f"{self.name}/_count").get("count", 0))

    def drop(self) -> None:
        if self.exists():
            self._call("DELETE", self.name)

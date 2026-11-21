"""LanceDB adapter (ADR-0028): embedded local tables (Lance columnar format), or LanceDB Cloud via
``url="db://<database>"`` with an ``api_key`` (and ``region``).

Columns: ``id``, ``vector`` (fixed-size float32), the document JSON, content hash, model id, the flat
filter fields (``filename``, ``artifact_id``, ``page``, ``section``, ``parent_id``, pre-filtered in SQL)
and the remaining ``meta_*`` fields as JSON (filtered exactly in Python over an enlarged candidate set).
Scores are recomputed from the returned vectors, so they are exact cosine similarities / dot products.
"""

from __future__ import annotations

import json
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
    matches,
    normalize_filter,
    similarity,
)

#: With ``meta_*`` filters, candidates are fetched up to this multiple of top_k (at least 1000) and then
#: filtered exactly; collections smaller than that are filtered completely.
META_OVERFETCH = 50


def _sql_literal(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


class LanceDBStore(VectorStore):
    def __init__(self, spec: StoreSpec) -> None:
        super().__init__(spec)
        try:
            import lancedb
        except ImportError as exc:  # pragma: no cover - optional extra
            raise VectorStoreError(
                "STORE_NOT_INSTALLED", "LanceDB support needs 'lancedb' (agenticmind-vectorstores[lancedb])"
            ) from exc
        if spec.url:
            self.db = lancedb.connect(spec.url, api_key=spec.api_key, region=spec.region or "us-east-1")
        elif spec.local_path is not None:
            spec.local_path.mkdir(parents=True, exist_ok=True)
            self.db = lancedb.connect(str(spec.local_path))
        else:
            raise VectorStoreError("STORE_CONFIG_INVALID", "LanceDB needs a local path or a db:// URL")
        self.name = spec.collection

    def _names(self) -> list[str]:
        lister = getattr(self.db, "table_names", None) or self.db.list_tables
        result = lister()
        return list(getattr(result, "tables", result))

    def _table(self) -> Any:
        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"LanceDB table '{self.name}' does not exist")
        return self.db.open_table(self.name)

    def _dims(self, table: Any) -> int:
        return int(table.schema.field("vector").type.list_size)

    def exists(self) -> bool:
        return self.name in self._names()

    def ensure_collection(self, dimensions: int) -> None:
        import pyarrow as pa

        if self.exists():
            dims = self._dims(self.db.open_table(self.name))
            if dims != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Table '{self.name}' stores {dims}-d vectors but the model produces {dimensions}-d.",
                )
            return
        schema = pa.schema([
            pa.field("id", pa.string()), pa.field("vector", pa.list_(pa.float32(), dimensions)),
            pa.field("document", pa.string()), pa.field("content_hash", pa.string()),
            pa.field("embedding_model", pa.string()), pa.field("filename", pa.string()),
            pa.field("artifact_id", pa.string()), pa.field("page", pa.int64()), pa.field("section", pa.string()),
            pa.field("parent_id", pa.string()), pa.field("meta", pa.string()),
        ])  # fmt: skip
        self.db.create_table(self.name, schema=schema)

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids or not self.exists():
            return {}
        table = self.db.open_table(self.name)
        out: dict[str, tuple[str, str]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            where = "id IN (" + ", ".join(_sql_literal(i) for i in chunk) + ")"
            rows = table.search().where(where).select(["id", "content_hash", "embedding_model"]).limit(len(chunk))
            for row in rows.to_list():
                out[row["id"]] = (row["content_hash"] or "", row["embedding_model"] or "")
        return out

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        if not records:
            return
        rows = []
        for r in records:
            page = r.fields.get("page")
            rows.append({
                "id": r.id, "vector": [float(x) for x in r.vector],
                "document": json.dumps(r.document, ensure_ascii=False), "content_hash": r.content_hash,
                "embedding_model": r.embedding_model,
                **{k: (str(r.fields[k]) if k in r.fields else None) for k in ("filename", "artifact_id", "section",
                                                                           "parent_id")},
                "page": int(page) if isinstance(page, (int, float)) and not isinstance(page, bool) else None,
                "meta": json.dumps({k: v for k, v in r.fields.items() if k.startswith("meta_")}, sort_keys=True),
            })  # fmt: skip
        table = self._table()
        table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        flt = normalize_filter(filters)
        table = self._table()
        flat = {k: v for k, v in flt.items() if k in FILTER_FIELDS}
        meta = {k: v for k, v in flt.items() if k not in FILTER_FIELDS}
        limit = top_k
        if meta:
            limit = max(top_k * META_OVERFETCH, 1000)
        search = table.search(list(vector)).distance_type("cosine" if self.spec.distance == "cosine" else "dot")
        if flat:
            where = " AND ".join(f"{k} = {_sql_literal(v)}" for k, v in flat.items())
            search = search.where(where, prefilter=True)
        rows = search.limit(limit).to_list()
        hits = []
        for row in rows:
            if meta and not matches(json.loads(row.get("meta") or "{}"), meta):
                continue
            hits.append(
                StoreHit(
                    id=row["id"],
                    score=similarity(vector, row["vector"], self.spec.distance),
                    document=json.loads(row["document"]),
                    embedding_model=row.get("embedding_model"),
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def count(self) -> int:
        return int(self.db.open_table(self.name).count_rows()) if self.exists() else 0

    def drop(self) -> None:
        if self.exists():
            self.db.drop_table(self.name)

    def list_collections(self) -> list[str]:
        return self._names()

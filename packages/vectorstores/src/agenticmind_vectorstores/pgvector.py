"""PostgreSQL + pgvector adapter (``dsn`` secret, e.g. ``postgresql://user:pass@host:5432/db``).

One table per collection: ``am_<collection>`` with ``id text PK, embedding vector(d), document jsonb,
fields jsonb, content_hash text, embedding_model text``. Requires the ``vector`` extension (created
if the role may do so). Uses only psycopg (vectors are passed as ``'[...]'::vector`` literals).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter


def table_name(name: str) -> str:
    clean = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return ("am_" + clean)[:63]


def _vec(v: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


class PgVectorStore(VectorStore):
    def __init__(self, spec: StoreSpec) -> None:
        super().__init__(spec)
        if not spec.dsn:
            raise VectorStoreError("STORE_CONFIG_INVALID", "pgvector needs a connection string (dsn secret)")
        try:
            import psycopg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional extra
            raise VectorStoreError(
                "STORE_NOT_INSTALLED", "pgvector support needs 'psycopg' (agenticmind-vectorstores[pgvector])"
            ) from exc
        self.table = table_name(spec.collection)

    def _connect(self) -> Any:
        import psycopg

        try:
            return psycopg.connect(self.spec.dsn, autocommit=True, connect_timeout=10)
        except psycopg.OperationalError as exc:
            raise VectorStoreError("STORE_UNAVAILABLE", f"Cannot connect to PostgreSQL: {exc}", retryable=True) from exc

    def _ident(self) -> Any:
        from psycopg import sql

        return sql.Identifier(self.table)

    def exists(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT to_regclass(%s)", (self.table,)).fetchone()
            return bool(row and row[0])

    def ensure_collection(self, dimensions: int) -> None:
        from psycopg import sql

        with self._connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} (id text PRIMARY KEY, embedding vector({}) NOT NULL, "
                    "document jsonb NOT NULL, fields jsonb NOT NULL DEFAULT '{{}}', content_hash text, "
                    "embedding_model text)"
                ).format(self._ident(), sql.Literal(dimensions))
            )
            row = conn.execute(
                "SELECT atttypmod FROM pg_attribute WHERE attrelid = %s::regclass AND attname = 'embedding'",
                (self.table,),
            ).fetchone()
            if row and row[0] not in (None, -1) and row[0] != dimensions:
                raise VectorStoreError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    f"Table '{self.table}' stores {row[0]}-d vectors but the model produces {dimensions}-d.",
                )

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        from psycopg import sql

        if not ids or not self.exists():
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                sql.SQL("SELECT id, content_hash, embedding_model FROM {} WHERE id = ANY(%s)").format(self._ident()),
                (list(ids),),
            ).fetchall()
        return {r[0]: (r[1] or "", r[2] or "") for r in rows}

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        from psycopg import sql

        query = sql.SQL(
            "INSERT INTO {} (id, embedding, document, fields, content_hash, embedding_model) "
            "VALUES (%s, %s::vector, %s::jsonb, %s::jsonb, %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "embedding = EXCLUDED.embedding, document = EXCLUDED.document, fields = EXCLUDED.fields, "
            "content_hash = EXCLUDED.content_hash, embedding_model = EXCLUDED.embedding_model"
        ).format(self._ident())
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany(
                query,
                [
                    (
                        r.id,
                        _vec(r.vector),
                        json.dumps(r.document),
                        json.dumps(r.fields),
                        r.content_hash,
                        r.embedding_model,
                    )
                    for r in records
                ],
            )

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        from psycopg import sql

        if not self.exists():
            raise VectorStoreError("COLLECTION_NOT_FOUND", f"Collection '{self.spec.collection}' does not exist")
        flt = normalize_filter(filters)
        score = "1 - (embedding <=> %s::vector)" if self.spec.distance == "cosine" else "-(embedding <#> %s::vector)"
        order = "embedding <=> %s::vector" if self.spec.distance == "cosine" else "embedding <#> %s::vector"
        where = sql.SQL("WHERE fields @> %s::jsonb") if flt else sql.SQL("")
        q = sql.SQL(
            "SELECT id, {score} AS score, document, embedding_model FROM {table} {where} ORDER BY {order} LIMIT %s"
        ).format(score=sql.SQL(score), table=self._ident(), where=where, order=sql.SQL(order))
        params: list[Any] = [_vec(vector)]
        if flt:
            params.append(json.dumps(flt))
        params += [_vec(vector), top_k]
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [StoreHit(id=r[0], score=float(r[1]), document=r[2], embedding_model=r[3]) for r in rows]

    def count(self) -> int:
        from psycopg import sql

        if not self.exists():
            return 0
        with self._connect() as conn:
            return int(conn.execute(sql.SQL("SELECT count(*) FROM {}").format(self._ident())).fetchone()[0])

    def drop(self) -> None:
        from psycopg import sql

        with self._connect() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(self._ident()))

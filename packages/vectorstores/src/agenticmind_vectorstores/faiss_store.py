"""FAISS adapter (ADR-0028): embedded, fully local, no server.

FAISS itself stores only vectors and integer ids, so each collection is two files under
``<local_path>/<collection>``:

* ``index.faiss`` - an ``IndexIDMap2`` over an exact ``IndexFlatIP`` (inner product; vectors are
  L2-normalised for cosine, so scores are exact cosine similarities / dot products);
* ``records.sqlite`` - the documents, content hashes, model ids and filterable fields, keyed by the same
  integer ids, plus the collection's dimensions and distance.

Filtered queries score the matching vectors exactly (reconstructed from the index), so filters never miss
results. A per-collection lock serialises writers inside the process.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .base import Scalar, StoreHit, StoreRecord, StoreSpec, VectorStore, VectorStoreError, normalize_filter

_LOCKS: dict[str, threading.RLock] = {}
_GUARD = threading.Lock()


def _lock(path: Path) -> threading.RLock:
    with _GUARD:
        return _LOCKS.setdefault(str(path), threading.RLock())


class FaissStore(VectorStore):
    def __init__(self, spec: StoreSpec) -> None:
        super().__init__(spec)
        try:
            import faiss  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as exc:  # pragma: no cover - optional extra
            raise VectorStoreError(
                "STORE_NOT_INSTALLED", "FAISS support needs 'faiss-cpu' (agenticmind-vectorstores[faiss])"
            ) from exc
        if spec.local_path is None:
            raise VectorStoreError("STORE_CONFIG_INVALID", "FAISS stores collections on local disk")
        self.dir = Path(spec.local_path) / spec.collection
        self.lock = _lock(self.dir)
        self._index: Any = None

    # -- files ------------------------------------------------------------------------------------
    @property
    def _db_path(self) -> Path:
        return self.dir / "records.sqlite"

    @property
    def _index_path(self) -> Path:
        return self.dir / "index.faiss"

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """A connection that commits on success and is always closed (Windows cannot delete open files)."""
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS records (rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, "
                "document TEXT NOT NULL, content_hash TEXT, embedding_model TEXT, fields TEXT NOT NULL)"
            )
            conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _meta(self, conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _load(self) -> Any:
        import faiss

        if self._index is None:
            if not self._index_path.exists():
                raise VectorStoreError(
                    "COLLECTION_NOT_FOUND", f"FAISS collection '{self.spec.collection}' does not exist"
                )
            self._index = faiss.read_index(str(self._index_path))
        return self._index

    def _prepare(self, vectors: Sequence[Sequence[float]]) -> Any:
        import faiss
        import numpy as np

        arr = np.asarray(vectors, dtype="float32")
        if self.spec.distance == "cosine":
            faiss.normalize_L2(arr)
        return arr

    # -- contract ---------------------------------------------------------------------------------
    def exists(self) -> bool:
        return self._index_path.exists() and self._db_path.exists()

    def ensure_collection(self, dimensions: int) -> None:
        import faiss

        with self.lock:
            if self.exists():
                with self._db() as conn:
                    dims = int(self._meta(conn, "dimensions") or 0)
                if dims != dimensions:
                    raise VectorStoreError(
                        "EMBEDDING_DIMENSION_MISMATCH",
                        f"Collection '{self.spec.collection}' stores {dims}-d vectors but the model produces "
                        f"{dimensions}-d. Re-index with 'recreate' or use another collection.",
                    )
                return
            self.dir.mkdir(parents=True, exist_ok=True)
            with self._db() as conn:
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('dimensions', ?)", (str(dimensions),))
                conn.execute("INSERT OR REPLACE INTO meta VALUES ('distance', ?)", (self.spec.distance,))
            self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimensions))
            faiss.write_index(self._index, str(self._index_path))

    def existing(self, ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not ids or not self.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        with self._db() as conn:
            for start in range(0, len(ids), 500):
                chunk = list(ids[start : start + 500])
                marks = ",".join("?" * len(chunk))
                for rid, h, m in conn.execute(
                    f"SELECT id, content_hash, embedding_model FROM records WHERE id IN ({marks})", chunk
                ):
                    out[rid] = (h or "", m or "")
        return out

    def upsert(self, records: Sequence[StoreRecord]) -> None:
        import faiss
        import numpy as np

        if not records:
            return
        with self.lock:
            index = self._load()
            with self._db() as conn:
                rowids = []
                for r in records:
                    row = conn.execute("SELECT rowid FROM records WHERE id = ?", (r.id,)).fetchone()
                    payload = (json.dumps(r.document, ensure_ascii=False), r.content_hash, r.embedding_model,
                               json.dumps(r.fields, sort_keys=True))  # fmt: skip
                    if row:
                        conn.execute(
                            "UPDATE records SET document=?, content_hash=?, embedding_model=?, fields=? WHERE rowid=?",
                            (*payload, row[0]),
                        )
                        rowids.append(row[0])
                    else:
                        cur = conn.execute(
                            "INSERT INTO records (id, document, content_hash, embedding_model, fields) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (r.id, *payload),
                        )
                        rowids.append(cur.lastrowid)
            ids = np.asarray(rowids, dtype="int64")
            index.remove_ids(faiss.IDSelectorBatch(ids))
            index.add_with_ids(self._prepare([r.vector for r in records]), ids)
            faiss.write_index(index, str(self._index_path))

    def query(self, vector: Sequence[float], top_k: int, filters: Mapping[str, Scalar] | None = None) -> list[StoreHit]:
        import numpy as np

        flt = normalize_filter(filters)
        with self.lock:
            index = self._load()
            q = self._prepare([vector])
            with self._db() as conn:
                if flt:
                    where = " AND ".join("json_extract(fields, ?) = ?" for _ in flt)
                    params: list[Any] = []
                    for k, v in flt.items():
                        params += [f'$."{k}"', int(v) if isinstance(v, bool) else v]
                    allowed = [r[0] for r in conn.execute(f"SELECT rowid FROM records WHERE {where}", params)]
                    if not allowed:
                        return []
                    vecs = np.vstack([index.reconstruct(int(i)) for i in allowed])
                    scores = vecs @ q[0]
                    order = np.argsort(-scores)[:top_k]
                    pairs = [(allowed[i], float(scores[i])) for i in order]
                else:
                    if index.ntotal == 0:
                        return []
                    scores, found = index.search(q, min(top_k, index.ntotal))
                    pairs = [(int(i), float(s)) for i, s in zip(found[0], scores[0], strict=True) if i != -1]
                hits = []
                for rowid, score in pairs:
                    row = conn.execute(
                        "SELECT id, document, embedding_model FROM records WHERE rowid = ?", (rowid,)
                    ).fetchone()
                    if row:
                        hits.append(StoreHit(id=row[0], score=score, document=json.loads(row[1]),
                                             embedding_model=row[2]))  # fmt: skip
        return hits

    def count(self) -> int:
        if not self.exists():
            return 0
        with self._db() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def drop(self) -> None:
        with self.lock:
            self._index = None
            if self.dir.exists():
                shutil.rmtree(self.dir, ignore_errors=True)

    def list_collections(self) -> list[str]:
        root = self.dir.parent
        return sorted(p.name for p in root.iterdir() if (p / "index.faiss").exists()) if root.is_dir() else []

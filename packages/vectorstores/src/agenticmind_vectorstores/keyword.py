"""Local BM25 keyword index (SQLite FTS5), kept alongside a vector collection (ADR-0015).

One SQLite file per collection holds every indexed chunk: an FTS5 table for BM25 ranking (Porter
stemming, Unicode folding) and a plain table with the full document JSON, content hash and the same flat
filter fields the vector stores use. Keyword hits therefore return complete documents with the same IDs as
vector hits, which is what hybrid fusion needs.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Scalar, VectorStoreError, normalize_filter

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_WORD = re.compile(r"\w+", re.UNICODE)
#: Words too common to help ranking; dropping them keeps OR-queries focused.
_STOP = frozenset(
    "a an and are as at be but by can did do does for from had has have how i if in into is it its me my no "
    "not of on or our so than that the their them then there these they this to was we were what when where "
    "which who why will with you your".split()
)
MAX_QUERY_TERMS = 32


@dataclass(frozen=True)
class KeywordRecord:
    id: str
    text: str
    document: dict[str, Any]
    content_hash: str
    fields: dict[str, Scalar]


@dataclass(frozen=True)
class KeywordHit:
    id: str
    score: float
    document: dict[str, Any]


def index_path(root: Path, backend: str, collection: str, namespace: str | None = None) -> Path:
    name = "__".join(_SAFE.sub("_", part) for part in (backend, collection, namespace or "") if part)
    return root / f"{name[:180]}.sqlite"


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms joined by OR (no user-controlled syntax)."""
    terms: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word not in _STOP and word not in terms:
            terms.append(word)
    if not terms:  # a query made only of stop words: search for them anyway
        terms = list(dict.fromkeys(_WORD.findall(text.lower())))
    terms = terms[:MAX_QUERY_TERMS]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms) if terms else None


class KeywordIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS docs (id TEXT PRIMARY KEY, content_hash TEXT, document TEXT, fields TEXT)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, body, "
            "tokenize='porter unicode61 remove_diacritics 2')"
        )
        return conn

    def exists(self) -> bool:
        return self.path.exists() and self.count() > 0

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT count(*) FROM docs").fetchone()[0]

    def existing(self, ids: list[str]) -> dict[str, str]:
        if not ids or not self.path.exists():
            return {}
        out: dict[str, str] = {}
        with self._lock, self._connect() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" * len(chunk))
                for row_id, h in conn.execute(f"SELECT id, content_hash FROM docs WHERE id IN ({marks})", chunk):
                    out[row_id] = h
        return out

    def upsert(self, records: list[KeywordRecord]) -> None:
        if not records:
            return
        with self._lock, self._connect() as conn:
            for r in records:
                conn.execute("DELETE FROM fts WHERE id = ?", (r.id,))
                conn.execute("INSERT INTO fts (id, body) VALUES (?, ?)", (r.id, r.text))
                conn.execute(
                    "INSERT OR REPLACE INTO docs (id, content_hash, document, fields) VALUES (?, ?, ?, ?)",
                    (r.id, r.content_hash, json.dumps(r.document, ensure_ascii=False), json.dumps(r.fields)),
                )

    def drop(self) -> None:
        if self.path.exists():
            with self._lock:
                self.path.unlink()

    def search(self, query: str, k: int, filters: Mapping[str, Scalar] | None = None) -> list[KeywordHit]:
        """BM25 search; ``score`` is positive (higher = more relevant)."""
        wanted = normalize_filter(filters)
        match = fts_query(query)
        if match is None or not self.path.exists():
            return []
        # filters are applied after ranking, so fetch extra candidates when filtering
        limit = k * 10 if wanted else k
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute(
                    "SELECT f.id, -bm25(fts) AS score, d.document, d.fields FROM fts f JOIN docs d ON d.id = f.id "
                    "WHERE fts MATCH ? ORDER BY score DESC LIMIT ?",
                    (match, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise VectorStoreError("KEYWORD_INDEX_ERROR", f"Keyword search failed: {exc}") from exc
        hits: list[KeywordHit] = []
        for row_id, score, document, fields in rows:
            if wanted:
                values = json.loads(fields)
                if any(values.get(key) != value for key, value in wanted.items()):
                    continue
            hits.append(KeywordHit(id=row_id, score=float(score), document=json.loads(document)))
            if len(hits) >= k:
                break
        return hits

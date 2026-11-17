"""Memory access for node handlers (ADR-0012).

Two deliberately separate kinds of memory:

* **Conversation state** (short-term): the recent turns of one conversation, keyed by a
  ``conversation_id`` supplied with the run. Used to give a model context across chat turns.
* **Long-term memories**: individual facts, written only by explicit Remember / Save Memory steps that
  require consent, grouped in a ``namespace`` (e.g. one per end user) and searchable later.

Both are scoped to the run's project by the runtime, carry a retention period and can be listed and
deleted through the API. Nodes see memory only with ``Permissions(memory=True)``.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

#: Conversation ids and namespaces are opaque client-chosen keys; keep them boring.
KEY_PATTERN = r"^[A-Za-z0-9_.:@-]{1,128}$"
_KEY_RE = re.compile(KEY_PATTERN)
MAX_MEMORY_CHARS = 4_000
MAX_MESSAGE_CHARS = 20_000


def valid_key(value: str) -> bool:
    return isinstance(value, str) and bool(_KEY_RE.match(value))


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: str | None = None
    run_id: str | None = None


class MemoryRecord(BaseModel):
    id: str
    namespace: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict, description="run_id / node_id that wrote it")
    created_at: str | None = None
    expires_at: str | None = None


class MemoryHit(BaseModel):
    memory: MemoryRecord
    score: float
    match: Literal["keyword", "recent"] = Field(
        default="keyword", description="keyword: shares terms with the query. recent: filled in by recency."
    )


class MemoryAccess(Protocol):
    """Project-scoped memory operations. Implementations purge expired entries on access."""

    def recent_turns(self, conversation_id: str, limit: int) -> list[ConversationTurn]: ...

    def append_turns(
        self, conversation_id: str, turns: list[ConversationTurn], *, run_id: str | None, retention_days: int
    ) -> None: ...

    def remember(
        self,
        namespace: str,
        content: str,
        *,
        metadata: dict[str, Any] | None,
        source: dict[str, Any],
        retention_days: int,
    ) -> MemoryRecord: ...

    def search(self, namespace: str, query: str, limit: int) -> list[MemoryHit]:
        """Keyword matches first; remaining slots are filled with the most recent memories."""
        ...


_WORD = re.compile(r"[\w']+", re.UNICODE)
_STOP = frozenset(
    "a an and are as at be but by did do does for from had has have how i in is it its me my of on or our so "
    "that the their them there these they this to was we were what when where which who why will with you "
    "your".split()
)


def _stem(word: str) -> str:
    """Very light suffix stripping so 'traveling', 'travels' and 'travel' share a term."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if len(word) > 3 and word[-1] == word[-2] else word  # travell -> travel


def tokenize(text: str) -> list[str]:
    words = [w.lower().strip("'") for w in _WORD.findall(text)]
    return [_stem(w) for w in words if w and w not in _STOP]


def keyword_scores(query: str, documents: list[str]) -> list[float]:
    """BM25-style relevance of each document to the query (0 = no shared terms).

    Deterministic and dependency-free; semantic (embedding) recall can be added behind the same API.
    """
    q = set(tokenize(query))
    if not q or not documents:
        return [0.0] * len(documents)
    docs = [tokenize(d) for d in documents]
    n = len(docs)
    avg = sum(len(d) for d in docs) / n or 1.0
    df = Counter(t for d in docs for t in set(d))
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
        scores.append(round(s, 4))
    return scores


def _now() -> datetime:
    return datetime.now(UTC)


def expiry_for(retention_days: int) -> str | None:
    """ISO timestamp ``retention_days`` from now, or ``None`` (keep until deleted) for 0."""
    return (_now() + timedelta(days=retention_days)).isoformat() if retention_days > 0 else None


def _alive(expires_at: str | None) -> bool:
    return expires_at is None or expires_at > _now().isoformat()


class InMemoryMemory:
    """Reference ``MemoryAccess`` kept in process memory (tests, CLI). Not persistent."""

    def __init__(self) -> None:
        self.turns: dict[str, list[tuple[ConversationTurn, str | None]]] = {}
        self.memories: dict[str, MemoryRecord] = {}
        self._seq = 0

    def recent_turns(self, conversation_id: str, limit: int) -> list[ConversationTurn]:
        alive = [t for t, exp in self.turns.get(conversation_id, []) if _alive(exp)]
        return alive[-limit:] if limit > 0 else []

    def append_turns(
        self, conversation_id: str, turns: list[ConversationTurn], *, run_id: str | None, retention_days: int
    ) -> None:
        stamp, exp = _now().isoformat(), expiry_for(retention_days)
        self.turns.setdefault(conversation_id, []).extend(
            (t.model_copy(update={"created_at": stamp, "run_id": run_id}), exp) for t in turns
        )

    def remember(
        self,
        namespace: str,
        content: str,
        *,
        metadata: dict[str, Any] | None,
        source: dict[str, Any],
        retention_days: int,
    ) -> MemoryRecord:
        self._seq += 1
        record = MemoryRecord(
            id=f"mem_{self._seq:016x}",
            namespace=namespace,
            content=content,
            metadata=dict(metadata or {}),
            source=dict(source),
            created_at=_now().isoformat(),
            expires_at=expiry_for(retention_days),
        )
        self.memories[record.id] = record
        return record

    def search(self, namespace: str, query: str, limit: int) -> list[MemoryHit]:
        pool = [m for m in self.memories.values() if m.namespace == namespace and _alive(m.expires_at)]
        return rank(pool, query, limit)


def rank(pool: list[MemoryRecord], query: str, limit: int) -> list[MemoryHit]:
    """Keyword matches by relevance (newest first on ties), then the newest other memories up to ``limit``.

    Filling with recent memories matters because keyword overlap misses paraphrases ("trip" vs
    "traveling"); a user's memory set is usually small enough that recent facts are worth showing.
    """
    newest = sorted(pool, key=lambda m: m.created_at or "", reverse=True)
    scores = keyword_scores(query, [m.content for m in newest])
    hits = [MemoryHit(memory=m, score=s) for m, s in zip(newest, scores, strict=True) if s > 0]
    hits.sort(key=lambda h: h.score, reverse=True)  # stable: ties stay newest-first
    matched = {h.memory.id for h in hits}
    hits += [MemoryHit(memory=m, score=0.0, match="recent") for m in newest if m.id not in matched]
    return hits[:limit]

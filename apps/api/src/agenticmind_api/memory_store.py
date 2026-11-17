"""Database-backed memory (ADR-0012): conversation messages and long-term memories per project.

Expired entries are purged whenever a project's memory is read, so retention holds even without a
background job. Everything is keyed by project id (the isolation boundary until workspaces exist).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from agenticmind_node_sdk import ConversationTurn, MemoryHit, MemoryRecord, rank
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from .db import ConversationMessageRow, MemoryRow, utcnow

#: Upper bound on memories scored per search (keyword ranking happens in Python).
SEARCH_POOL = 2_000


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def _expires(days: int) -> datetime | None:
    return utcnow() + timedelta(days=days) if days > 0 else None


def _record(row: MemoryRow) -> MemoryRecord:
    return MemoryRecord(
        id=row.id,
        namespace=row.namespace,
        content=row.content,
        metadata=row.meta or {},
        source=row.source or {},
        created_at=_iso(row.created_at),
        expires_at=_iso(row.expires_at),
    )


class SqlMemory:
    """``MemoryAccess`` for one project."""

    def __init__(self, sf: sessionmaker, project_id: str) -> None:
        self._sf = sf
        self.project_id = project_id

    def _purge(self, s: Session) -> None:
        now = utcnow()
        for model in (ConversationMessageRow, MemoryRow):
            s.execute(
                delete(model).where(
                    model.project_id == self.project_id, model.expires_at.is_not(None), model.expires_at <= now
                )
            )

    # -- node-facing (MemoryAccess) --------------------------------------------------------------
    def recent_turns(self, conversation_id: str, limit: int) -> list[ConversationTurn]:
        if limit <= 0:
            return []
        with self._sf.begin() as s:
            self._purge(s)
            rows = list(
                s.scalars(
                    select(ConversationMessageRow)
                    .where(
                        ConversationMessageRow.project_id == self.project_id,
                        ConversationMessageRow.conversation_id == conversation_id,
                    )
                    .order_by(ConversationMessageRow.id.desc())
                    .limit(limit)
                )
            )
        return [
            ConversationTurn(role=r.role, content=r.content, created_at=_iso(r.created_at), run_id=r.run_id)
            for r in reversed(rows)
        ]

    def append_turns(
        self, conversation_id: str, turns: list[ConversationTurn], *, run_id: str | None, retention_days: int
    ) -> None:
        expires = _expires(retention_days)
        with self._sf.begin() as s:
            for t in turns:
                s.add(
                    ConversationMessageRow(
                        project_id=self.project_id,
                        conversation_id=conversation_id,
                        role=t.role,
                        content=t.content,
                        run_id=run_id,
                        expires_at=expires,
                    )
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
        row = MemoryRow(
            id=f"mem_{uuid.uuid4().hex[:16]}",
            project_id=self.project_id,
            namespace=namespace,
            content=content,
            meta=dict(metadata or {}),
            source=dict(source),
            created_at=utcnow(),
            expires_at=_expires(retention_days),
        )
        with self._sf.begin() as s:
            s.add(row)
        return _record(row)

    def search(self, namespace: str, query: str, limit: int) -> list[MemoryHit]:
        with self._sf.begin() as s:
            self._purge(s)
            rows = list(
                s.scalars(
                    select(MemoryRow)
                    .where(MemoryRow.project_id == self.project_id, MemoryRow.namespace == namespace)
                    .order_by(MemoryRow.created_at.desc())
                    .limit(SEARCH_POOL)
                )
            )
        return rank([_record(r) for r in rows], query, limit)

    # -- management (API) ------------------------------------------------------------------------
    def list_memories(self, namespace: str | None = None, query: str | None = None) -> list[MemoryRecord]:
        with self._sf.begin() as s:
            self._purge(s)
            stmt = select(MemoryRow).where(MemoryRow.project_id == self.project_id)
            if namespace:
                stmt = stmt.where(MemoryRow.namespace == namespace)
            if query:
                like = f"%{query.replace('%', '').replace('_', '')}%"
                stmt = stmt.where(or_(MemoryRow.content.ilike(like), MemoryRow.namespace.ilike(like)))
            rows = list(s.scalars(stmt.order_by(MemoryRow.created_at.desc()).limit(1000)))
        return [_record(r) for r in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self._sf.begin() as s:
            res = s.execute(delete(MemoryRow).where(MemoryRow.project_id == self.project_id, MemoryRow.id == memory_id))
        return res.rowcount > 0

    def clear_memories(self, namespace: str | None = None) -> int:
        with self._sf.begin() as s:
            stmt = delete(MemoryRow).where(MemoryRow.project_id == self.project_id)
            if namespace:
                stmt = stmt.where(MemoryRow.namespace == namespace)
            return s.execute(stmt).rowcount

    def list_conversations(self) -> list[dict[str, Any]]:
        m = ConversationMessageRow
        with self._sf.begin() as s:
            self._purge(s)
            rows = s.execute(
                select(m.conversation_id, func.count(m.id), func.min(m.created_at), func.max(m.created_at))
                .where(m.project_id == self.project_id)
                .group_by(m.conversation_id)
                .order_by(func.max(m.created_at).desc())
            ).all()
        return [
            {"id": cid, "messages": count, "started_at": _iso(first), "updated_at": _iso(last)}
            for cid, count, first, last in rows
        ]

    def conversation(self, conversation_id: str) -> list[ConversationTurn]:
        return self.recent_turns(conversation_id, 10_000)

    def delete_conversation(self, conversation_id: str) -> int:
        m = ConversationMessageRow
        with self._sf.begin() as s:
            return s.execute(
                delete(m).where(m.project_id == self.project_id, m.conversation_id == conversation_id)
            ).rowcount


class SqlMemoryStore:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory

    def scoped(self, project_id: str) -> SqlMemory:
        return SqlMemory(self._sf, project_id)

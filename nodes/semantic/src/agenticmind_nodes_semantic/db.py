"""Read-only database access for semantic nodes (ADR-0018).

* **SQLite**: an uploaded project file, copied into node storage (keyed by its SHA-256) and opened with
  ``mode=ro``; a progress handler enforces the timeout.
* **PostgreSQL**: ``dsn`` from a secret reference; every session runs with
  ``default_transaction_read_only=on`` and ``statement_timeout``.

Writes are therefore refused twice: by the SQL policy (``policy.py``) and by the connection itself.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agenticmind_node_sdk import DatabaseHandle, NodeContext, NodeError, TransientNodeError

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    duration_ms: float


@dataclass
class Table:
    schema: str | None
    name: str
    kind: str = "table"
    columns: list[dict[str, Any]] = field(default_factory=list)
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)
    row_count: int | None = None
    sample_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema": self.schema,
            "name": self.name,
            "kind": self.kind,
            "columns": self.columns,
            "foreign_keys": self.foreign_keys,
        }
        if self.row_count is not None:
            out["row_count"] = self.row_count
        if self.sample_rows:
            out["sample_rows"] = self.sample_rows
        return out


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)  # dates, decimals, UUIDs ...


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Database:
    """One read-only connection for the duration of a node call (use via ``open_database``)."""

    def __init__(self, handle: DatabaseHandle, conn: Any) -> None:
        self.handle = handle
        self.conn = conn

    @property
    def dialect(self) -> str:
        return "sqlite" if self.handle.engine == "sqlite" else "postgres"

    # -- queries ----------------------------------------------------------------------------------
    def _run(self, sql: str, params: tuple = (), *, limit: int | None = None) -> QueryResult:
        t0 = time.perf_counter()
        limit = limit or self.handle.max_rows
        try:
            if self.handle.engine == "sqlite":
                deadline = time.monotonic() + self.handle.timeout_seconds
                self.conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
                cur = self.conn.execute(sql, params)
            else:
                cur = self.conn.cursor()
                cur.execute(sql, params)
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(limit + 1) if cur.description else []
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc):
                raise NodeError("SQL_TIMEOUT", f"Query exceeded {self.handle.timeout_seconds:g}s") from exc
            raise NodeError("SQL_EXECUTION_ERROR", str(exc)) from exc
        except sqlite3.Error as exc:
            raise NodeError("SQL_EXECUTION_ERROR", str(exc)) from exc
        except Exception as exc:  # psycopg errors (optional dependency)
            name = type(exc).__name__
            if name == "QueryCanceled":
                raise NodeError("SQL_TIMEOUT", f"Query exceeded {self.handle.timeout_seconds:g}s") from exc
            if name in ("OperationalError", "InterfaceError"):
                raise TransientNodeError("DATABASE_UNAVAILABLE", str(exc).strip()) from exc
            if hasattr(exc, "sqlstate"):
                raise NodeError("SQL_EXECUTION_ERROR", str(exc).strip()) from exc
            raise
        finally:
            if self.handle.engine == "sqlite":
                self.conn.set_progress_handler(None, 0)
            else:
                self.conn.rollback()
        rows = [{c: _json_safe(v) for c, v in zip(columns, r, strict=False)} for r in fetched[:limit]]
        return QueryResult(columns, rows, len(fetched) > limit, round((time.perf_counter() - t0) * 1000, 1))

    def query(self, sql: str, *, limit: int | None = None) -> QueryResult:
        return self._run(sql, limit=limit)

    # -- introspection ----------------------------------------------------------------------------
    def tables(self) -> list[Table]:
        return self._sqlite_tables() if self.handle.engine == "sqlite" else self._pg_tables()

    def _allowed(self, schema: str | None, name: str) -> bool:
        if not self.handle.schemas:
            return True
        return (schema or "main") in self.handle.schemas or (
            f"{schema}.{name}" if schema else name
        ) in self.handle.schemas

    def _sqlite_tables(self) -> list[Table]:
        rows = self.conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        out = []
        for name, kind in rows:
            if not self._allowed(None, name):
                continue
            t = Table(schema=None, name=name, kind=kind)
            for _cid, col, ctype, notnull, _default, pk in self.conn.execute(f"PRAGMA table_info({quote_ident(name)})"):
                t.columns.append({"name": col, "type": ctype or "", "nullable": not notnull, "primary_key": bool(pk)})
            fks: dict[int, dict[str, Any]] = {}
            for fk_id, _seq, ref, frm, to, *_ in self.conn.execute(f"PRAGMA foreign_key_list({quote_ident(name)})"):
                entry = fks.setdefault(fk_id, {"columns": [], "ref_table": ref, "ref_columns": []})
                entry["columns"].append(frm)
                entry["ref_columns"].append(to)
            t.foreign_keys = list(fks.values())
            out.append(t)
        return out

    def _pg_tables(self) -> list[Table]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_schema NOT IN %s AND table_schema NOT LIKE 'pg_temp%%' ORDER BY 1, 2",
            (SYSTEM_SCHEMAS,),
        )
        tables = {
            (s, n): Table(schema=s, name=n, kind="view" if k == "VIEW" else "table")
            for s, n, k in cur.fetchall()
            if self._allowed(s, n)
        }
        cur.execute(
            "SELECT table_schema, table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema NOT IN %s ORDER BY table_schema, table_name, ordinal_position",
            (SYSTEM_SCHEMAS,),
        )
        for s, n, col, dtype, nullable in cur.fetchall():
            if (s, n) in tables:
                tables[(s, n)].columns.append(
                    {"name": col, "type": dtype, "nullable": nullable == "YES", "primary_key": False}
                )
        cur.execute(
            "SELECT kcu.table_schema, kcu.table_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema WHERE tc.constraint_type = 'PRIMARY KEY'"
        )
        for s, n, col in cur.fetchall():
            for c in tables.get((s, n), Table(None, "")).columns:
                if c["name"] == col:
                    c["primary_key"] = True
        cur.execute(
            "SELECT kcu.table_schema, kcu.table_name, kcu.column_name, "
            "ccu.table_schema, ccu.table_name, ccu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema "
            "JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
            "AND tc.table_schema = ccu.table_schema WHERE tc.constraint_type = 'FOREIGN KEY'"
        )
        for s, n, col, rs, rn, rc in cur.fetchall():
            if (s, n) in tables:
                tables[(s, n)].foreign_keys.append({"columns": [col], "ref_table": f"{rs}.{rn}", "ref_columns": [rc]})
        self.conn.rollback()
        return list(tables.values())

    def row_count(self, table: Table) -> int:
        result = self._run(f"SELECT COUNT(*) AS n FROM {self.qualified(table)}", limit=1)
        return int(result.rows[0]["n"]) if result.rows else 0

    def qualified(self, table: Table) -> str:
        return f"{quote_ident(table.schema)}.{quote_ident(table.name)}" if table.schema else quote_ident(table.name)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


_COPY_LOCKS: dict[str, threading.Lock] = {}
_COPY_GUARD = threading.Lock()


def _sqlite_copy(ctx: NodeContext, artifact_id: str) -> Path:
    """Copy an uploaded SQLite file into node storage once (content-addressed; safe under concurrency)."""
    info = ctx.artifact_info(artifact_id)
    target = ctx.storage_path("databases") / f"{info.sha256}.sqlite"
    with _COPY_GUARD:
        lock = _COPY_LOCKS.setdefault(str(target), threading.Lock())
    with lock:  # parallel nodes on the same file: one copies, the others wait and reuse it
        if not target.exists():
            data = ctx.read_artifact(artifact_id)
            if not data.startswith(b"SQLite format 3\x00"):
                raise NodeError("DATABASE_INVALID_FILE", f"'{info.filename}' is not a SQLite database")
            tmp = target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
    return target


def _connect(ctx: NodeContext, handle: DatabaseHandle) -> Any:
    if handle.engine == "sqlite":
        if handle.artifact_id:
            path = _sqlite_copy(ctx, handle.artifact_id)
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
        elif handle.dsn is not None:  # operator-provided path via a secret, still read-only
            path_text = ctx.secret(handle.dsn).removeprefix("sqlite:///")
            conn = sqlite3.connect(f"file:{Path(path_text).as_posix()}?mode=ro", uri=True, check_same_thread=False)
        else:
            raise NodeError("DATABASE_NOT_CONFIGURED", "Select an uploaded SQLite file or a dsn secret")
        conn.execute("PRAGMA query_only = ON")
        return conn
    if handle.dsn is None:
        raise NodeError("DATABASE_NOT_CONFIGURED", "PostgreSQL needs a dsn secret reference")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise NodeError(
            "DATABASE_DRIVER_MISSING", "Install agenticmind-nodes-semantic[postgres] for PostgreSQL"
        ) from exc
    timeout_ms = int(handle.timeout_seconds * 1000)
    try:
        return psycopg.connect(
            ctx.secret(handle.dsn),
            connect_timeout=10,
            options=f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}",
        )
    except psycopg.OperationalError as exc:
        raise TransientNodeError("DATABASE_UNAVAILABLE", str(exc).strip()) from exc


async def open_database(ctx: NodeContext, handle: DatabaseHandle) -> Database:
    return Database(handle, await asyncio.to_thread(_connect, ctx, handle))


def schema_text(tables: list[Table], *, max_chars: int = 12_000) -> str:
    """Compact, LLM-friendly schema description."""
    lines = []
    for t in tables:
        fk = {
            c: f"{f['ref_table']}.{rc}"
            for f in t.foreign_keys
            for c, rc in zip(f["columns"], f["ref_columns"], strict=False)
        }
        cols = ", ".join(
            f"{c['name']} {c['type']}".strip()
            + (" PK" if c["primary_key"] else "")
            + (f" -> {fk[c['name']]}" if c["name"] in fk else "")
            for c in t.columns
        )
        rows = f"  -- {t.row_count} rows" if t.row_count is not None else ""
        lines.append(f"{t.kind.upper()} {t.qualified} ({cols}){rows}")
    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n… (schema truncated)"

"""Read-only SQL policy (ADR-0018): parse, check and bound a query before it reaches the database.

Rules: exactly one statement; a query (SELECT / set operation, CTEs allowed) with no writes anywhere,
including data-modifying CTEs and ``SELECT … INTO``; no denied functions (file access, sleeps, admin);
every referenced table exists in the inspected schema and in the allow-list; the row count is capped with
LIMIT. The connection is read-only as well, so a policy gap still cannot write.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from agenticmind_node_sdk import NodeError
from sqlglot import exp
from sqlglot.errors import ParseError

DENIED_FUNCTIONS = frozenset(
    {
        # file system / large objects / remote access
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_get",
        "dblink", "dblink_exec", "readfile", "writefile", "load_extension", "edit",
        # stalling / admin
        "pg_sleep", "pg_sleep_for", "pg_sleep_until", "pg_terminate_backend", "pg_cancel_backend",
        "pg_reload_conf", "set_config", "pg_advisory_lock", "pg_advisory_xact_lock",
    }
)  # fmt: skip
WRITE_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter, exp.Command, exp.Into)


@dataclass
class CheckedSQL:
    sql: str
    tables: list[str]
    limit: int
    #: True when the policy added or lowered the LIMIT (so reaching it means rows were cut off)
    capped: bool = False
    notes: list[str] = field(default_factory=list)


def _name(table: exp.Table) -> str:
    return f"{table.db}.{table.name}".lower() if table.db else table.name.lower()


def check_sql(
    sql: str, *, dialect: str, known_tables: set[str], max_rows: int, allowed: set[str] | None = None
) -> CheckedSQL:
    """Return the bounded SQL to run, or raise ``NodeError`` with an actionable message.

    ``known_tables`` holds lower-case ``name`` and ``schema.name`` forms of every inspected table.
    """
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise NodeError("SQL_EMPTY", "The SQL is empty")
    try:
        statements = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except ParseError as exc:
        raise NodeError("SQL_PARSE_ERROR", f"Could not parse the SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1:
        raise NodeError("SQL_MULTIPLE_STATEMENTS", "Send exactly one SQL statement")
    stmt = statements[0]
    if not isinstance(stmt, (exp.Select, exp.SetOperation)):
        raise NodeError("SQL_NOT_READ_ONLY", f"Only SELECT queries are allowed (got {stmt.key.upper()})")
    write = stmt.find(*WRITE_NODES)
    if write is not None:
        raise NodeError(
            "SQL_NOT_READ_ONLY", f"The query contains a write ({write.key.upper()}); only reads are allowed"
        )
    for func in stmt.find_all(exp.Func):
        name = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if name in DENIED_FUNCTIONS:
            raise NodeError("SQL_FUNCTION_DENIED", f"The function {name}() is not allowed")

    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    referenced: list[str] = []
    for table in stmt.find_all(exp.Table):
        name = _name(table)
        if not table.name or name in ctes or name in referenced:
            continue
        referenced.append(name)
    unknown = [t for t in referenced if t not in known_tables]
    if unknown:
        raise NodeError(
            "SQL_UNKNOWN_TABLE", f"Unknown table(s): {', '.join(unknown)}. Use only tables from the schema."
        )
    if allowed is not None:
        blocked = [t for t in referenced if t not in allowed and t.split(".")[-1] not in allowed]
        if blocked:
            raise NodeError("SQL_TABLE_NOT_ALLOWED", f"Table(s) not allowed by policy: {', '.join(blocked)}")

    notes: list[str] = []
    existing = stmt.args.get("limit")
    limit_value = None
    if isinstance(existing, exp.Limit) and isinstance(existing.expression, exp.Literal) and existing.expression.is_int:
        limit_value = int(existing.expression.this)
    if isinstance(stmt, exp.Select):
        if limit_value is None or limit_value > max_rows:
            if limit_value is not None:
                notes.append(f"LIMIT {limit_value} lowered to {max_rows}")
            stmt = stmt.limit(max_rows)
        bounded = stmt
    else:  # set operations: bound by wrapping
        bounded = exp.select("*").from_(stmt.subquery("q")).limit(max_rows)
    capped = limit_value is None or limit_value > max_rows
    return CheckedSQL(
        sql=bounded.sql(dialect=dialect),
        tables=referenced,
        limit=min(limit_value or max_rows, max_rows),
        capped=capped,
        notes=notes,
    )


def known_table_names(tables: list) -> set[str]:
    names: set[str] = set()
    for t in tables:
        names.add(t.name.lower())
        if t.schema:
            names.add(f"{t.schema}.{t.name}".lower())
    return names

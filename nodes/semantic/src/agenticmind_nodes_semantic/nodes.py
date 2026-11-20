"""Semantic data nodes (blueprint §9, ADR-0018): Database Connector, Schema Inspector, Table Profiler,
SQL Validator/Executor, NL-to-SQL and a read-only SQL tool for agents."""

from __future__ import annotations

import asyncio
import fnmatch
import re
from typing import Any, Literal

from agenticmind_node_sdk import (
    DatabaseHandle,
    ExecutionPolicy,
    InputPort,
    ModelUsage,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    ToolHandle,
    TransientNodeError,
    define_node,
    secret_field,
    ui,
)
from agenticmind_node_sdk.handles import TOOL_NAME_PATTERN, handle_value
from agenticmind_providers import (
    ChatMessage,
    ChatProviderName,
    ChatRequest,
    ProviderError,
    ToolSpec,
    check_chat_provider,
    get_provider,
)
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from pydantic import Field, model_validator

from .db import Database, Table, open_database, quote_ident, schema_text
from .policy import check_sql, known_table_names

PACKAGE = "agenticmind-nodes-semantic"
#: What it takes to open a database: the dsn secret, an uploaded SQLite file copied into node storage.
DB_PERMISSIONS = Permissions(database=True, credentials=True, artifacts=True, filesystem=True)


async def _with_db(ctx: NodeContext, raw: Any, fn):  # noqa: ANN001, ANN202
    handle = DatabaseHandle.model_validate(raw)
    db = await open_database(ctx, handle)
    try:
        return await asyncio.to_thread(fn, db)
    finally:
        db.close()


# ---- Database Connector -------------------------------------------------------------------------
class DatabaseConfig(NodeConfig):
    engine: Literal["sqlite", "postgresql"] = Field(
        default="sqlite", description="sqlite: an uploaded .sqlite/.db file (read-only). postgresql: a dsn secret."
    )
    file: list[str] = Field(
        default_factory=list,
        description="SQLite: the uploaded database file (pick one).",
        json_schema_extra=ui(widget="artifacts"),
    )
    dsn: SecretRef | None = secret_field("PostgreSQL connection string secret, e.g. ANALYTICS_DB_DSN")
    schemas: list[str] = Field(
        default_factory=list,
        description="Limit queries to these schemas or schema.table names (empty = everything readable).",
        json_schema_extra=ui(advanced=True),
    )
    max_rows: int = Field(default=500, ge=1, le=100_000, description="Upper bound on rows any query returns.")
    timeout_seconds: float = Field(default=15, gt=0, le=300, description="Upper bound on any query's duration.")

    @model_validator(mode="after")
    def _check(self) -> DatabaseConfig:
        if self.engine == "sqlite" and len(self.file) > 1:
            raise ValueError("pick a single SQLite file")
        if self.engine == "postgresql" and self.dsn is None:
            raise ValueError('postgresql needs a dsn secret reference (e.g. {"$secret": "ANALYTICS_DB_DSN"})')
        return self


@define_node(
    type="agenticmind.semantic.database",
    version="1.0.0",
    display_name="Database Connector",
    category="semantic",
    package=PACKAGE,
    description="A read-only database connection (SQLite file or PostgreSQL via a secret) for the nodes it feeds.",
    outputs=[OutputPort(name="database", type=PortType.DATABASE)],
    config=DatabaseConfig,
    permissions=Permissions(credentials=True),
    tags=["database", "semantic"],
)
async def database(ctx: NodeContext, inputs, config: DatabaseConfig) -> NodeResult:
    if config.engine == "sqlite" and not config.file and config.dsn is None:
        raise NodeError("DATABASE_NOT_CONFIGURED", "Select an uploaded SQLite file (or set a dsn secret)")
    handle = DatabaseHandle(
        engine=config.engine,
        artifact_id=config.file[0] if config.file else None,
        dsn=config.dsn,
        schemas=config.schemas,
        max_rows=config.max_rows,
        timeout_seconds=config.timeout_seconds,
    )
    return NodeResult(outputs={"database": handle_value(handle)})


# ---- Schema Inspector ---------------------------------------------------------------------------
class SchemaConfig(NodeConfig):
    tables: list[str] = Field(default_factory=list, description="Only these tables (glob patterns; empty = all).")
    row_counts: bool = Field(default=True, description="Count rows per table.")
    sample_rows: int = Field(default=0, ge=0, le=10, description="Example rows per table (0 = none).")
    max_tables: int = Field(default=200, ge=1, le=2000, json_schema_extra=ui(advanced=True))


def _select(tables: list[Table], patterns: list[str]) -> list[Table]:
    if not patterns:
        return tables
    return [
        t
        for t in tables
        if any(
            fnmatch.fnmatch(t.qualified.lower(), p.lower()) or fnmatch.fnmatch(t.name.lower(), p.lower())
            for p in patterns
        )
    ]


@define_node(
    type="agenticmind.semantic.schema_inspector",
    version="1.0.0",
    display_name="Schema Inspector",
    category="semantic",
    package=PACKAGE,
    description="Discovers tables, columns, keys and relationships (and optionally row counts and examples).",
    inputs=[InputPort(name="database", type=PortType.DATABASE)],
    outputs=[
        OutputPort(
            name="schema", type=PortType.JSON, description="{engine, tables: [{name, columns, foreign_keys, …}]}"
        ),
        OutputPort(name="text", type=PortType.STRING, description="Compact schema description for prompts"),
        OutputPort(name="table_count", type=PortType.NUMBER),
    ],
    config=SchemaConfig,
    permissions=DB_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["database", "semantic"],
)
async def schema_inspector(ctx: NodeContext, inputs, config: SchemaConfig) -> NodeResult:
    def inspect(db: Database) -> tuple[str, list[Table]]:
        tables = _select(db.tables(), config.tables)[: config.max_tables]
        for t in tables:
            if config.row_counts:
                t.row_count = db.row_count(t)
            if config.sample_rows:
                t.sample_rows = db.query(f"SELECT * FROM {db.qualified(t)}", limit=config.sample_rows).rows
        return db.handle.engine, tables

    engine, tables = await _with_db(ctx, inputs["database"], inspect)
    await ctx.emit("schema_inspected", {"tables": len(tables), "engine": engine})
    return NodeResult(
        outputs={
            "schema": {"engine": engine, "tables": [t.to_json() for t in tables]},
            "text": schema_text(tables),
            "table_count": len(tables),
        }
    )


# ---- Table Profiler -----------------------------------------------------------------------------
class ProfileConfig(NodeConfig):
    table: str = Field(min_length=1, description="Table to profile (name or schema.name).")
    columns: list[str] = Field(default_factory=list, description="Only these columns (empty = all).")
    top_values: int = Field(default=5, ge=0, le=50, description="Most frequent values per column.")
    max_rows_scanned: int = Field(
        default=100_000,
        ge=100,
        le=10_000_000,
        description="Profile at most this many rows.",
        json_schema_extra=ui(advanced=True),
    )


_NUMERIC = re.compile(r"int|real|num|float|double|dec|money", re.I)


def profile_table(db: Database, config: ProfileConfig) -> dict[str, Any]:
    tables = db.tables()
    target = next((t for t in tables if config.table.lower() in (t.name.lower(), t.qualified.lower())), None)
    if target is None:
        raise NodeError(
            "TABLE_NOT_FOUND", f"No table '{config.table}'. Available: {', '.join(t.qualified for t in tables[:30])}"
        )
    cols = [c for c in target.columns if not config.columns or c["name"] in config.columns]
    source = f"(SELECT * FROM {db.qualified(target)} LIMIT {int(config.max_rows_scanned)}) AS s"
    total = int(db.query(f"SELECT COUNT(*) AS n FROM {source}", limit=1).rows[0]["n"])
    profiles = []
    for col in cols:
        q = quote_ident(col["name"])
        numeric = bool(_NUMERIC.search(col["type"] or ""))
        stats_sql = (
            f"SELECT COUNT({q}) AS non_null, COUNT(DISTINCT {q}) AS distinct_values, "
            + (
                f"MIN({q}) AS min, MAX({q}) AS max, AVG({q}) AS mean"
                if numeric
                else f"MIN(LENGTH({q})) AS min_length, MAX(LENGTH({q})) AS max_length"
            )
            + f" FROM {source}"
        )
        stats = db.query(stats_sql, limit=1).rows[0]
        entry: dict[str, Any] = {
            "name": col["name"],
            "type": col["type"],
            "null_rate": round(1 - (stats["non_null"] or 0) / total, 4) if total else None,
            "distinct": stats["distinct_values"],
            **{
                k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in stats.items()
                if k not in ("non_null", "distinct_values")
            },
        }
        if config.top_values:
            top = db.query(
                f"SELECT {q} AS value, COUNT(*) AS count FROM {source} WHERE {q} IS NOT NULL "
                f"GROUP BY {q} ORDER BY COUNT(*) DESC LIMIT {int(config.top_values)}",
                limit=config.top_values,
            ).rows
            entry["top_values"] = top
        profiles.append(entry)
    return {"table": target.qualified, "rows_profiled": total, "columns": profiles}


def _profile_text(p: dict[str, Any]) -> str:
    lines = [f"{p['table']}: {p['rows_profiled']} rows profiled"]
    for c in p["columns"]:
        extra = (
            f"min {c.get('min')}, max {c.get('max')}, mean {c.get('mean')}"
            if "mean" in c
            else f"length {c.get('min_length')}-{c.get('max_length')}"
        )
        top = ", ".join(f"{t['value']} ({t['count']})" for t in c.get("top_values", [])[:3])
        lines.append(
            f"- {c['name']} ({c['type']}): {c['null_rate']:.0%} null, {c['distinct']} distinct, {extra}"
            + (f"; top: {top}" if top else "")
        )
    return "\n".join(lines)


@define_node(
    type="agenticmind.semantic.table_profiler",
    version="1.0.0",
    display_name="Table Profiler",
    category="semantic",
    package=PACKAGE,
    description="Summarises a table's data quality and distributions: nulls, distinct values, ranges, top values.",
    inputs=[InputPort(name="database", type=PortType.DATABASE)],
    outputs=[
        OutputPort(name="profile", type=PortType.JSON),
        OutputPort(name="text", type=PortType.STRING, description="Readable summary"),
    ],
    config=ProfileConfig,
    permissions=DB_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=300, max_attempts=1, idempotent=True),
    tags=["database", "semantic"],
)
async def table_profiler(ctx: NodeContext, inputs, config: ProfileConfig) -> NodeResult:
    profile = await _with_db(ctx, inputs["database"], lambda db: profile_table(db, config))
    return NodeResult(outputs={"profile": profile, "text": _profile_text(profile)})


# ---- SQL Validator / Executor -------------------------------------------------------------------
class SQLConfig(NodeConfig):
    mode: Literal["execute", "validate"] = Field(
        default="execute", description="validate: check against the policy only. execute: check, then run read-only."
    )
    allowed_tables: list[str] = Field(
        default_factory=list, description="Extra allow-list for this node (empty = every table the connection allows)."
    )
    max_rows: int | None = Field(default=None, ge=1, le=100_000, description="Lower the connection's row cap.")


def run_checked(
    db: Database, sql: str, *, max_rows: int | None = None, allowed: list[str] | None = None, execute: bool = True
) -> dict[str, Any]:
    tables = db.tables()
    cap = min(max_rows or db.handle.max_rows, db.handle.max_rows)
    checked = check_sql(
        sql,
        dialect=db.dialect,
        known_tables=known_table_names(tables),
        max_rows=cap,
        allowed={a.lower() for a in allowed} if allowed else None,
    )
    out: dict[str, Any] = {
        "sql": checked.sql,
        "tables": checked.tables,
        "notes": checked.notes,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "truncated": False,
    }
    if execute:
        result = db.query(checked.sql, limit=cap)
        out.update(
            columns=result.columns,
            rows=result.rows,
            row_count=len(result.rows),
            truncated=result.truncated or (checked.capped and len(result.rows) >= checked.limit),
            duration_ms=result.duration_ms,
        )
    return out


@define_node(
    type="agenticmind.semantic.sql_executor",
    version="1.0.0",
    display_name="SQL Validator / Executor",
    category="semantic",
    package=PACKAGE,
    description="Checks SQL against a read-only policy (one SELECT, known tables, no writes, row cap) and runs it.",
    inputs=[
        InputPort(name="database", type=PortType.DATABASE),
        InputPort(name="sql", type=PortType.STRING),
    ],
    outputs=[
        OutputPort(name="rows", type=PortType.JSON, description="List of row objects"),
        OutputPort(name="columns", type=PortType.JSON),
        OutputPort(
            name="result", type=PortType.JSON, description="{sql (as run), tables, row_count, truncated, notes}"
        ),
    ],
    config=SQLConfig,
    permissions=DB_PERMISSIONS,
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=1, idempotent=True),
    tags=["database", "semantic"],
)
async def sql_executor(ctx: NodeContext, inputs, config: SQLConfig) -> NodeResult:
    out = await _with_db(
        ctx,
        inputs["database"],
        lambda db: run_checked(
            db, inputs["sql"], max_rows=config.max_rows, allowed=config.allowed_tables, execute=config.mode == "execute"
        ),
    )
    await ctx.emit("sql_checked", {"sql": out["sql"], "rows": out["row_count"], "truncated": out["truncated"]})
    summary = {k: v for k, v in out.items() if k not in ("rows", "columns")}
    return NodeResult(outputs={"rows": out["rows"], "columns": out["columns"], "result": summary})


# ---- NL-to-SQL ----------------------------------------------------------------------------------
class NLToSQLConfig(NodeConfig):
    provider: ChatProviderName = Field(default="openrouter")
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))
    instructions: str = Field(
        default="Prefer simple, correct SQL. Use only the tables and columns listed. Qualify ambiguous columns. "
        "For top-N questions, include rows tied at the cut-off (e.g. with a window function) "
        "or say that there are ties.",
        json_schema_extra=ui(widget="textarea"),
    )
    attempts: int = Field(default=3, ge=1, le=6, description="Tries: errors are shown to the model to fix.")
    execute: bool = Field(default=True, description="Run the validated SQL and return rows.")
    max_rows: int = Field(default=200, ge=1, le=10_000)
    temperature: float = Field(default=0.0, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=4000, ge=64, le=100_000, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> NLToSQLConfig:
        check_chat_provider(self.provider, self.api_key, getattr(self, "base_url", None) or "-")
        return self


SUBMIT_SQL = ToolSpec(
    name="submit_sql",
    description="Submit one read-only SQL query that answers the question, or explain why it cannot be answered",
    parameters={
        "type": "object",
        "properties": {
            "answerable": {
                "type": "boolean",
                "description": "false if answering needs data that is not in the schema, or asks to change data",
            },
            "sql": {"type": "string", "description": "A single SELECT statement (empty when not answerable)"},
            "explanation": {
                "type": "string",
                "description": "One sentence: what the query computes, or why it cannot be answered",
            },
        },
        "required": ["answerable", "sql", "explanation"],
    },
)
#: Feedback for write attempts: refuse plainly instead of substituting a different query.
WRITE_GUIDANCE = (
    "This connection is read-only and nothing can be changed. If the question asks to modify data, "
    "set answerable=false and explain that; do not substitute a different query."
)


@define_node(
    type="agenticmind.semantic.nl_to_sql",
    version="1.0.0",
    display_name="NL-to-SQL",
    category="semantic",
    package=PACKAGE,
    description="Turns a question into SQL grounded in the schema, validates it read-only, runs it, retries on errors.",
    inputs=[
        InputPort(name="question", type=PortType.STRING),
        InputPort(name="database", type=PortType.DATABASE),
        InputPort(name="schema", type=PortType.STRING, required=False, description="Schema text (default: inspect)"),
        InputPort(name="context", type=PortType.STRING, required=False, description="Business definitions, metrics"),
    ],
    outputs=[
        OutputPort(name="sql", type=PortType.STRING),
        OutputPort(name="rows", type=PortType.JSON),
        OutputPort(name="result", type=PortType.JSON, description="{answer, sql, rows, columns, row_count, attempts}"),
    ],
    config=NLToSQLConfig,
    permissions=Permissions(network=True, database=True, credentials=True, artifacts=True, filesystem=True),
    execution=ExecutionPolicy(timeout_seconds=300, max_attempts=1, idempotent=True),
    tags=["database", "semantic", "llm"],
)
async def nl_to_sql(ctx: NodeContext, inputs, config: NLToSQLConfig) -> NodeResult:
    question = inputs["question"].strip()
    if not question:
        raise NodeError("QUESTION_EMPTY", "The question is empty")
    handle = DatabaseHandle.model_validate(inputs["database"])
    db = await open_database(ctx, handle)
    try:
        schema = inputs.get("schema") or await asyncio.to_thread(lambda: schema_text(db.tables()))
        provider = get_provider(config.provider)
        api_key = ctx.secret(config.api_key) if config.api_key else None
        system = (
            f"You write {('SQLite' if handle.engine == 'sqlite' else 'PostgreSQL')} SQL for analytics questions. "
            f"Read-only: one SELECT statement (CTEs allowed). {config.instructions}\n\nSchema:\n{schema}"
            + (f"\n\nBusiness context:\n{inputs['context']}" if inputs.get("context") else "")
        )
        attempts: list[dict[str, Any]] = []
        tokens_in = tokens_out = 0
        cost: float | None = None
        for attempt in range(1, config.attempts + 1):
            user = f"Question: {question}"
            if attempts:
                user += "\n\nEarlier attempts failed:\n" + "\n".join(
                    f"- {a['sql']}\n  error: {a['error']}" for a in attempts
                )
                if any("SQL_NOT_READ_ONLY" in (a["error"] or "") for a in attempts):
                    user += f"\n\n{WRITE_GUIDANCE}"
            try:
                resp = await provider.chat(
                    ChatRequest(
                        model=config.model,
                        system=system,
                        messages=[ChatMessage(role="user", content=user)],
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        tools=[SUBMIT_SQL],
                        tool_choice="required",
                    ),
                    api_key=api_key,
                    base_url=config.base_url,
                )
            except ProviderError as exc:
                if exc.retryable:
                    raise TransientNodeError(exc.code, str(exc)) from exc
                raise NodeError(exc.code, str(exc)) from exc
            tokens_in += resp.usage.input_tokens
            tokens_out += resp.usage.output_tokens
            if resp.usage.estimated_cost_usd is not None:
                cost = (cost or 0.0) + resp.usage.estimated_cost_usd
            call = next((c for c in resp.tool_calls if c.name == SUBMIT_SQL.name), None)
            if call is None:
                reason = "ran out of output tokens" if resp.finish_reason == "length" else "returned no SQL"
                attempts.append({"sql": "", "error": f"the model {reason}"})
                continue
            sql = str(call.arguments.get("sql", ""))
            explanation = str(call.arguments.get("explanation", "")).strip()
            if call.arguments.get("answerable") is False or not sql.strip():
                # an honest "cannot answer" is a valid result, not a failure
                attempts.append({"sql": "", "error": None})
                await ctx.emit("sql_not_answerable", {"attempt": attempt})
                usage = ModelUsage(
                    provider=config.provider,
                    model=config.model,
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    estimated_cost_usd=cost,
                )
                result = {
                    "answer": explanation or "This question cannot be answered from the database.",
                    "sql": "",
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                    "truncated": False,
                    "answerable": False,
                    "attempts": attempts,
                }
                return NodeResult(outputs={"sql": "", "rows": [], "result": result}, usage=usage)
            try:
                out = await asyncio.to_thread(run_checked, db, sql, max_rows=config.max_rows, execute=config.execute)
            except NodeError as exc:
                attempts.append({"sql": sql, "error": f"{exc.code}: {exc.message}"})
                await ctx.emit("sql_attempt_failed", {"attempt": attempt, "code": exc.code})
                continue
            attempts.append({"sql": out["sql"], "error": None})
            usage = ModelUsage(
                provider=config.provider,
                model=config.model,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                estimated_cost_usd=cost,
            )
            rows_note = ""
            if config.execute:
                plural = "s" if out["row_count"] != 1 else ""
                rows_note = f" ({out['row_count']} row{plural}{', truncated' if out['truncated'] else ''})"
            result = {
                "answer": explanation + rows_note,
                "sql": out["sql"],
                "columns": out["columns"],
                "rows": out["rows"][:50],
                "row_count": out["row_count"],
                "truncated": out["truncated"],
                "answerable": True,
                "attempts": attempts,
            }
            return NodeResult(outputs={"sql": out["sql"], "rows": out["rows"], "result": result}, usage=usage)
        last = attempts[-1]["error"] if attempts else "none"
        raise NodeError("NL_TO_SQL_FAILED", f"No valid query after {config.attempts} attempt(s); last error: {last}")
    finally:
        db.close()


# ---- SQL tool for agents ------------------------------------------------------------------------
class SQLToolConfig(NodeConfig):
    name: str = Field(default="query_database", pattern=TOOL_NAME_PATTERN)
    description: str = Field(
        default="Run one read-only SQL SELECT against the business database and get rows back. "
        "Only the tables below exist; errors explain what to fix.",
        max_length=2000,
        json_schema_extra=ui(widget="textarea"),
    )
    max_rows: int = Field(default=50, ge=1, le=1000)


@define_node(
    type="agenticmind.semantic.tool_sql",
    version="1.0.0",
    display_name="SQL Tool",
    category="tools",
    package=PACKAGE,
    description="Lets an Agent query a database read-only; the schema is included so it knows the tables.",
    inputs=[InputPort(name="database", type=PortType.DATABASE)],
    outputs=[OutputPort(name="tool", type=PortType.TOOL)],
    config=SQLToolConfig,
    permissions=DB_PERMISSIONS,
    tags=["tools", "database"],
)
async def sql_tool(ctx: NodeContext, inputs, config: SQLToolConfig) -> NodeResult:
    def describe(db: Database) -> tuple[str, str]:
        return db.dialect, schema_text(db.tables(), max_chars=6000)

    dialect, schema = await _with_db(ctx, inputs["database"], describe)
    handle = ToolHandle(
        tool_type="sql_query",
        name=config.name,
        description=f"{config.description}\nDialect: {dialect}.\n{schema}"[:2000],
        parameters={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One SELECT statement"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        binding={"database": inputs["database"], "max_rows": config.max_rows},
        permissions=["database", "credentials", "artifacts", "filesystem"],
    )
    return NodeResult(outputs={"tool": handle_value(handle)})

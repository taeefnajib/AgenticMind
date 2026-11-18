# Semantic data nodes

Read-only access to SQL databases for analytics graphs and agents (ADR-0018). Try it: **New** → *Ask your
database*, upload `examples/data/sales.sqlite`, select it in **Sales database**, then ask in **Chat**.

| Node | Does |
|---|---|
| Database Connector | Read-only connection: an uploaded SQLite file, or PostgreSQL via a `dsn` secret; row cap, timeout, schema allow-list |
| Schema Inspector | Tables, columns, keys, foreign keys, row counts, example rows, and a compact text form for prompts |
| Table Profiler | Null rate, distinct values, min/max/mean or text lengths, top values |
| SQL Validator / Executor | Checks SQL against the read-only policy, then runs it |
| NL-to-SQL | Question → SQL grounded in the schema (+ business context), validated, executed, retried on errors; can answer "not answerable" |
| SQL Tool | Gives an Agent a `query_database` tool with the schema in its description |

## The read-only policy

A query runs only if it is exactly one SELECT (CTEs and UNION allowed), contains no write anywhere
(including `SELECT … INTO` and data-modifying CTEs), calls no denied function (file access, sleeps,
admin), references only tables that exist and are allowed, and fits the row cap (LIMIT is added or
lowered). Independently, the connection itself is read-only, so a policy gap still cannot change data.
For PostgreSQL, also use a database role with read-only grants.

Error codes: `SQL_EMPTY`, `SQL_PARSE_ERROR`, `SQL_MULTIPLE_STATEMENTS`, `SQL_NOT_READ_ONLY`,
`SQL_FUNCTION_DENIED`, `SQL_UNKNOWN_TABLE`, `SQL_TABLE_NOT_ALLOWED`, `SQL_TIMEOUT`, `SQL_EXECUTION_ERROR`,
`DATABASE_NOT_CONFIGURED`, `DATABASE_INVALID_FILE`, `DATABASE_UNAVAILABLE`, `DATABASE_DRIVER_MISSING`,
`TABLE_NOT_FOUND`, `NL_TO_SQL_FAILED`, `QUESTION_EMPTY`.

## PostgreSQL

Set a secret such as `ANALYTICS_DB_DSN=postgresql://reader:…@host:5432/db` in `.env`, choose engine
`postgresql` and reference `{"$secret": "ANALYTICS_DB_DSN"}`. Run the opt-in test with
`AGENTICMIND_TEST_POSTGRES_DSN`.

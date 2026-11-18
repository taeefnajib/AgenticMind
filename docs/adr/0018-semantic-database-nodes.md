# ADR-0018: Semantic data nodes (M7a)

**Status:** Accepted

## Context
Blueprint §9 makes semantic engineering a capability family separate from RAG. Its node table starts with
nodes that need no external system: Database Connector, Schema Inspector, Table Profiler, NL-to-SQL and
SQL Validator/Executor "under explicit policies". The NVIDIA Auto Ontology adapter is a separate step
(M7b), because it integrates a deployed external service and must be pinned and contract-tested.

## Decision
- **New core package `agenticmind-nodes-semantic`** with a `Database` port type. `DatabaseHandle`
  (engine, uploaded SQLite artifact or `dsn` secret reference, schema allow-list, row cap, timeout) is a
  handle, so its secret reference is delegated along edges like other handles; values never travel.
- **Read-only twice.** `policy.check_sql` (sqlglot, pinned 30.29.0) allows exactly one query (SELECT or
  set operation, CTEs allowed). It rejects writes anywhere, including `SELECT … INTO` and data-modifying
  CTEs, rejects denied functions (file access, sleeps, admin), requires every table to exist in the
  inspected schema and in the allow-list, and adds or lowers LIMIT. Independently, connections are
  read-only: SQLite files are opened `mode=ro` with `query_only` (a content-addressed copy in node storage,
  with a per-file lock for parallel nodes), and Postgres sessions use `default_transaction_read_only=on` and
  `statement_timeout`. A test sends a DELETE straight to the connection, bypassing the policy, and it fails.
- **Bounded.** Each query has a timeout (SQLite progress handler, Postgres `statement_timeout`) and a row
  cap; results report `truncated` only when the policy's own cap cut rows off.
- **Nodes:**
  - Database Connector.
  - Schema Inspector: tables, columns, primary and foreign keys, optional row counts and examples, plus a
    compact text form for prompts.
  - Table Profiler: null rate, distinct counts, ranges or lengths, top values over a bounded scan.
  - SQL Validator/Executor.
  - NL-to-SQL: a forced `submit_sql` call grounded in the schema and optional business context; policy and
    database errors go back to the model for up to N attempts. The model can declare a question
    `answerable=false` (data not in the schema, or a request to change data). That is a valid result, and
    after a blocked write the model is told to refuse rather than substitute a different query.
  - SQL Tool: an agent tool that embeds the schema in its description. Its implementation is registered
    through the `agenticmind.tools` entry point (ADR-0014), and the Agent gained the `artifacts` permission
    so it can open uploaded SQLite files.
- **Sample data:** `examples/data/sales.sqlite`, a deterministic fictional sales database built by
  `scripts/build_sample_db.py` (customers, products, orders, order items), and the *Ask your database*
  template.

## Verification
Live with Gemini on the sample database:
- "Which product category had the most revenue in 2025?" returned Office, 88,792.70, matching an
  independent query.
- The first top-3 answer hid a four-way tie; the tie guidance was added to the default instructions.
- "Delete all cancelled orders" was blocked; the model then substituted a SELECT, so the refusal path was
  added. It now refuses on the first attempt, and the data was never modified.
- An unanswerable question ("customer satisfaction") is now refused instead of guessed.

## Consequences
- Supported engines: SQLite (uploaded files) and PostgreSQL (opt-in live test via
  `AGENTICMIND_TEST_POSTGRES_DSN`); others can be added behind `db.py`.
- The read-only policy is a guard against mistakes and prompt injection, not a full SQL firewall; least
  privilege still belongs in the database (use a read-only role in the dsn).

## Revisit when
More engines are needed, column-level permissions or PII masking are required, or Auto Ontology (M7b)
provides semantic context for NL-to-SQL.

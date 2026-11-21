# ADR-0026: Data sources: popular databases, S3 storage, workspace secrets

**Status:** Accepted. This is step 2 of the "build any agentic workflow" plan (step 1 is ADR-0025).

## Context
Users asked to load files from S3 and to connect the Database Connector, with credentials, to Supabase,
Databricks and other popular databases. The connector handled only SQLite files and PostgreSQL through a
single DSN secret. Every credential had to be put in the server's `.env` by hand.

## Decision

### Database engines
- **`DatabaseHandle.engine`** now covers `sqlite`, `postgresql`, `mysql`, `sqlserver`, `snowflake`,
  `databricks`, `duckdb`, `clickhouse` and `bigquery`.
  - Connections come from a DSN secret or from structured fields: host, port, database, user, plus
    `password`, `token`, `private_key` or `service_account` *secret references*, and engine-specific fields
    such as Snowflake `account`/`warehouse`/`role`, Databricks `warehouse`/`catalog` and BigQuery
    `project`/`location`.
  - Adding enum options and optional fields is backward compatible, so the node stays at version 1.0.0.
  - The form shows only the fields for the chosen engine, through a new `show_when` UI hint (node SDK `ui()`,
    honoured by the web config form).

| engine | covers | how | read-only / bounded |
|---|---|---|---|
| postgresql | Supabase, Neon, RDS/Aurora, CockroachDB, Redshift | psycopg | read-only session + `statement_timeout` |
| mysql | MySQL, MariaDB, PlanetScale, TiDB | PyMySQL | `SET SESSION TRANSACTION READ ONLY`, `max_execution_time` / `max_statement_time`, rollback |
| sqlserver | SQL Server, Azure SQL | pymssql | policy + rollback after each query, driver timeout |
| snowflake | Snowflake (password, PAT or key pair) | snowflake-connector-python | policy + `STATEMENT_TIMEOUT_IN_SECONDS` |
| databricks | SQL warehouses (Unity Catalog) | SQL Statement Execution API (httpx) | policy + polling; cancelled on timeout |
| duckdb | `.duckdb` files, MotherDuck | duckdb | local: `read_only` and no external file access; interrupt on timeout |
| clickhouse | ClickHouse, ClickHouse Cloud | HTTP interface (httpx) | `readonly=1`, `max_execution_time`, server-side row cap |
| bigquery | BigQuery | REST `jobs.query`, signed service-account JWT | policy + `jobTimeoutMs`; cancelled on timeout |

- **Shared code paths:** HTTP engines and DuckDB expose a DB-API-like cursor, so `Database` runs queries,
  introspection, timeouts and row caps the same way for all engines.
  - Introspection uses `information_schema`, or `system.*` on ClickHouse.
  - Primary keys are read where they are exposed; foreign keys on SQLite, PostgreSQL and MySQL.
- **SQL policy:** it runs in each engine's sqlglot dialect, so the row cap becomes `TOP` on SQL Server.
  New denied functions:
  - MySQL `SLEEP`, `BENCHMARK` and `LOAD_FILE`;
  - SQL Server `OPENROWSET` and `xp_cmdshell`;
  - DuckDB `read_*`, `glob` and similar file readers;
  - ClickHouse `url`, `s3`, `file`, `remote`, `mysql` and other table functions;
  - Snowflake `SYSTEM$WAIT`, BigQuery `EXTERNAL_QUERY`.
- **Portable SQL:** SQL written by AgenticMind itself (table profiler, row counts) is PostgreSQL-style and
  translated per engine with sqlglot. NL-to-SQL prompts name the engine's SQL flavour.
- **Engines without a read-only session** (SQL Server, Snowflake, Databricks, BigQuery) rely on the policy.
  The node's help asks for a read-only login, role or grant.
- **Upstream docs checked in Nov 2026:**
  - Databricks Statement Execution API: `wait_timeout` 5–50 s, `INLINE`/`JSON_ARRAY`; a failed statement
    still answers HTTP 200.
  - ClickHouse HTTP settings, including `readonly` as a query parameter.
  - BigQuery `jobs.query` and `getQueryResults`, with the `bigquery.readonly` scope.

### S3 Loader (`agenticmind.data.s3_loader@1.0.0`)
- **Scope:** Amazon S3 and S3-compatible storage (MinIO, Cloudflare R2, Wasabi, B2...) through boto3, with
  path-style addressing when `endpoint_url` is set.
- **Auth:**
  - access-key secrets, with an optional session token;
  - the server's own AWS identity: env, `~/.aws`, instance or task role;
  - anonymous, for public buckets.
- **Selection:** explicit keys, or a prefix listing with file patterns. Limits on file count and per-file
  size. Unsupported files found by listing are skipped; named keys that fail are errors.
- **Parsing:** shares the Document Loader's code, including OCR. Documents carry `s3://bucket/key`, ETag and
  last-modified.
- **SSRF:** endpoints resolving to link-local or metadata addresses are refused, so the server's cloud
  credentials cannot be fetched through a crafted endpoint.

### Workspace secrets
- **Settings → Secrets** lets admins add or replace named secrets. Members see the names.
- **Storage:** AES-256-GCM, with the name as associated data. The key comes from `AGENTICMIND_SECRET_KEY` or
  `<data>/secret.key`, which is generated with mode 600.
- **Write-only:** no endpoint returns a value.
- **Resolution:** the environment first (operator override), then the workspace store. This happens behind
  the existing `SecretResolver`, so runs, deployments, evaluations and the AI assistant all see stored
  secrets and graphs keep using `{"$secret": "NAME"}`.
- **Config forms:** secret fields show whether a name is set, and where, but never the value.

## Verification
- **DuckDB, end to end through the engine:** inspect (with primary keys), profile, query (`LIMIT` added),
  writes refused by the connection, `read_csv` refused, a runaway query interrupted, an invalid file
  refused.
- **ClickHouse, Databricks, BigQuery against mocked HTTP APIs:** request shape, auth headers, `readonly=1`
  last, polling, type conversion, a JWT with the read-only scope, failures and timeouts mapped to node
  errors.
- **MySQL, SQL Server, Snowflake through fake drivers:** DSN parsing, TLS, read-only session statements,
  timeouts, Snowflake key-pair (PEM → DER).
- **SQL policy:** row caps per dialect and the new denied functions.
- **S3 against botocore's Stubber:** paginated listing, patterns, limits, CSV/Markdown parsing, access
  denied and missing keys, the SSRF guard, R2 and anonymous client options.
- **Secrets:** encryption at rest and name binding, environment precedence, write-only API, member
  restrictions, key file handling.

## Consequences
- **No live servers tested:** PostgreSQL, MySQL, SQL Server, Snowflake, Databricks, ClickHouse, BigQuery and
  MotherDuck were not run against real servers in CI (Docker was unavailable on the build machine).
  Opt-in live tests remain to be added per engine.
- **Larger install:** the API now ships PyMySQL, pymssql, the Snowflake connector, DuckDB, boto3 and
  cryptography.
- **Read-only depends on the account** for engines without a read-only session.
- **Not covered yet:** Oracle, Hive-metastore Databricks without Unity Catalog, and other OAuth flows.
- **Back up the key:** the secrets key must be backed up with the database; without it, stored secrets
  cannot be read.

## Revisit when
More engines (Oracle, Trino, Athena), OAuth flows, external secret managers (Vault, AWS Secrets Manager), or
write-capable "action" nodes are needed.

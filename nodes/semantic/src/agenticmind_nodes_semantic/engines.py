"""Database engines beyond SQLite/PostgreSQL (ADR-0026).

Every engine returns a DB-API-like connection (``cursor().execute/description/fetchmany``, ``rollback``,
``close``) so ``db.Database`` runs queries and introspection the same way for all of them:

=============  ==========================  =============================================================
engine         driver                      read-only and time bound
=============  ==========================  =============================================================
mysql          PyMySQL                     ``SET SESSION TRANSACTION READ ONLY``; ``max_execution_time``
                                           (MySQL) / ``max_statement_time`` (MariaDB); rollback after each
sqlserver      pymssql                     policy + rollback after each query; driver ``timeout``
snowflake      snowflake-connector-python  policy; ``STATEMENT_TIMEOUT_IN_SECONDS`` session parameter
duckdb         duckdb                      ``read_only=True`` and external file access off (local files);
                                           interrupt on timeout
databricks     SQL Statement Execution API policy; ``wait_timeout`` + polling, cancel on timeout
clickhouse     HTTP interface              ``readonly=1``, ``max_execution_time``, row cap on the server
bigquery       REST ``jobs.query``         policy; ``jobTimeoutMs``, cancel on timeout
=============  ==========================  =============================================================

Engines without a server-side read-only session (SQL Server, Snowflake, Databricks, BigQuery) rely on
the SQL policy; connect them with a read-only login/role (documented in the node's help).
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from agenticmind_node_sdk import DatabaseHandle, NodeContext, NodeError, TransientNodeError


@dataclass(frozen=True)
class EngineInfo:
    label: str
    #: sqlglot dialect used by the SQL policy and to translate portable SQL.
    dialect: str
    #: How prompts name the SQL flavour (NL-to-SQL).
    sql_name: str
    default_port: int | None = None


ENGINES: dict[str, EngineInfo] = {
    "sqlite": EngineInfo("SQLite", "sqlite", "SQLite"),
    "postgresql": EngineInfo("PostgreSQL", "postgres", "PostgreSQL", 5432),
    "mysql": EngineInfo("MySQL / MariaDB", "mysql", "MySQL", 3306),
    "sqlserver": EngineInfo("SQL Server", "tsql", "T-SQL (SQL Server)", 1433),
    "snowflake": EngineInfo("Snowflake", "snowflake", "Snowflake SQL"),
    "databricks": EngineInfo("Databricks", "databricks", "Databricks SQL"),
    "duckdb": EngineInfo("DuckDB", "duckdb", "DuckDB SQL"),
    "clickhouse": EngineInfo("ClickHouse", "clickhouse", "ClickHouse SQL", 8443),
    "bigquery": EngineInfo("BigQuery", "bigquery", "BigQuery GoogleSQL"),
}


def _secret(ctx: NodeContext, ref: Any) -> str | None:
    return ctx.secret(ref) if ref is not None else None


def _require(value: Any, what: str, engine: str) -> Any:
    if value in (None, ""):
        raise NodeError("DATABASE_NOT_CONFIGURED", f"{ENGINES[engine].label} needs {what}")
    return value


def parse_url(dsn: str) -> dict[str, Any]:
    """``scheme://user:password@host:port/database?k=v`` → parts (for drivers that take no URL)."""
    u = urlparse(dsn)
    return {
        "host": u.hostname,
        "port": u.port,
        "user": unquote(u.username) if u.username else None,
        "password": unquote(u.password) if u.password else None,
        "database": u.path.lstrip("/") or None,
    }


def _driver_missing(engine: str, module: str) -> NodeError:
    return NodeError(
        "DATABASE_DRIVER_MISSING",
        f"{ENGINES[engine].label} needs the '{module}' driver: pip install 'agenticmind-nodes-semantic[{engine}]'",
    )


# ---- DB-API drivers -----------------------------------------------------------------------------
def connect_mysql(ctx: NodeContext, h: DatabaseHandle) -> Any:
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise _driver_missing("mysql", "pymysql") from exc
    parts = parse_url(ctx.secret(h.dsn)) if h.dsn is not None else {}
    params = {
        "host": parts.get("host") or _require(h.host, "a host", "mysql"),
        "port": parts.get("port") or h.port or 3306,
        "user": parts.get("user") or _require(h.user, "a user", "mysql"),
        "password": parts.get("password") or _secret(ctx, h.password) or "",
        "database": parts.get("database") or h.database,
        "connect_timeout": 10,
        "read_timeout": int(h.timeout_seconds) + 10,
        "autocommit": False,
        "charset": "utf8mb4",
    }
    if h.ssl != "disable":
        # TLS on; without a CA bundle PyMySQL encrypts but does not verify (like libpq sslmode=require)
        params["ssl"] = {"check_hostname": False}
    try:
        conn = pymysql.connect(**params)
    except pymysql.err.OperationalError as exc:
        raise TransientNodeError("DATABASE_UNAVAILABLE", str(exc)) from exc
    cur = conn.cursor()
    cur.execute("SET SESSION TRANSACTION READ ONLY")
    ms = int(h.timeout_seconds * 1000)
    try:
        cur.execute(f"SET SESSION max_execution_time = {ms}")  # MySQL
    except pymysql.err.MySQLError:
        try:
            cur.execute(f"SET SESSION max_statement_time = {h.timeout_seconds:g}")  # MariaDB
        except pymysql.err.MySQLError:
            pass
    conn.commit()
    return conn


def connect_sqlserver(ctx: NodeContext, h: DatabaseHandle) -> Any:
    try:
        import pymssql
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise _driver_missing("sqlserver", "pymssql") from exc
    parts = parse_url(ctx.secret(h.dsn)) if h.dsn is not None else {}
    try:
        return pymssql.connect(
            server=parts.get("host") or _require(h.host, "a host", "sqlserver"),
            port=str(parts.get("port") or h.port or 1433),
            user=parts.get("user") or _require(h.user, "a user", "sqlserver"),
            password=parts.get("password") or _secret(ctx, h.password) or "",
            database=parts.get("database") or h.database or "",
            timeout=int(h.timeout_seconds),
            login_timeout=10,
            autocommit=False,
            appname="AgenticMind",
        )
    except pymssql.OperationalError as exc:
        raise TransientNodeError("DATABASE_UNAVAILABLE", str(exc)) from exc


def _pem_to_der(pem: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    return key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def connect_snowflake(ctx: NodeContext, h: DatabaseHandle) -> Any:
    try:
        import snowflake.connector
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise _driver_missing("snowflake", "snowflake-connector-python") from exc
    params: dict[str, Any] = {
        "account": _require(h.account, "an account identifier (e.g. myorg-myaccount)", "snowflake"),
        "user": _require(h.user, "a user", "snowflake"),
        "warehouse": h.warehouse,
        "database": h.database,
        "schema": h.default_schema,
        "role": h.role,
        "login_timeout": 20,
        "application": "AgenticMind",
        "session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": int(h.timeout_seconds), "QUERY_TAG": "agenticmind"},
    }
    if h.private_key is not None:  # key-pair authentication (recommended for service users)
        params["private_key"] = _pem_to_der(ctx.secret(h.private_key))
    else:  # a password or a programmatic access token
        params["password"] = _require(_secret(ctx, h.password), "a password/token or private key secret", "snowflake")
    params = {k: v for k, v in params.items() if v is not None}
    try:
        return snowflake.connector.connect(**params)
    except snowflake.connector.errors.DatabaseError as exc:
        raise NodeError("DATABASE_UNAVAILABLE", str(exc).strip()) from exc


class _InterruptingConnection:
    """Wraps a DuckDB connection: a timer interrupts queries that run past the time limit."""

    def __init__(self, conn: Any, timeout: float) -> None:
        self._conn = conn
        self._timeout = timeout

    def cursor(self) -> Any:
        return _InterruptingCursor(self._conn, self._timeout)

    def rollback(self) -> None:  # read-only: nothing to roll back
        pass

    def close(self) -> None:
        self._conn.close()


class _InterruptingCursor:
    def __init__(self, conn: Any, timeout: float) -> None:
        self._conn = conn
        self._timeout = timeout
        self._cur: Any = None

    def execute(self, sql: str, params: tuple = ()) -> None:
        timer = threading.Timer(self._timeout, self._conn.interrupt)
        timer.start()
        try:
            self._cur = self._conn.execute(sql, list(params))
        except Exception as exc:
            if "interrupt" in str(exc).lower():
                raise NodeError("SQL_TIMEOUT", f"Query exceeded {self._timeout:g}s") from exc
            raise
        finally:
            timer.cancel()

    @property
    def description(self) -> Any:
        return self._cur.description if self._cur is not None else None

    def fetchmany(self, n: int) -> list[tuple]:
        return self._cur.fetchmany(n)

    def fetchall(self) -> list[tuple]:
        return self._cur.fetchall()


def connect_duckdb(ctx: NodeContext, h: DatabaseHandle, local_path: str | None) -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise _driver_missing("duckdb", "duckdb") from exc
    if local_path is not None:
        # read-only, and no access to other files (read_csv/read_parquet/COPY/ATTACH of arbitrary paths)
        conn = duckdb.connect(local_path, read_only=True, config={"enable_external_access": False})
    elif h.token is not None:  # MotherDuck
        name = _require(h.database, "a MotherDuck database name", "duckdb")
        conn = duckdb.connect(f"md:{name}", config={"motherduck_token": ctx.secret(h.token)})
    else:
        raise NodeError("DATABASE_NOT_CONFIGURED", "DuckDB needs an uploaded .duckdb file or a MotherDuck token")
    return _InterruptingConnection(conn, h.timeout_seconds)


# ---- HTTP engines (a DB-API-like cursor over JSON results) ---------------------------------------
class _HttpCursor:
    def __init__(self, run: Any) -> None:
        self._run = run
        self.description: list[tuple] | None = None
        self._rows: list[tuple] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        if params:
            raise NodeError("SQL_EXECUTION_ERROR", "Parameters are not supported for this engine")
        columns, rows = self._run(sql)
        self.description = [(c, None, None, None, None, None, None) for c in columns]
        self._rows = rows

    def fetchmany(self, n: int) -> list[tuple]:
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self) -> list[tuple]:
        out, self._rows = self._rows, []
        return out


class HttpConnection:
    def __init__(self, run: Any, close: Any = None) -> None:
        self._run = run
        self._close = close

    def cursor(self) -> _HttpCursor:
        return _HttpCursor(self._run)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        if self._close:
            self._close()


def _http_error(engine: str, resp: Any) -> NodeError:
    label = ENGINES[engine].label
    if resp.status_code in (401, 403):
        return NodeError("DATABASE_AUTH_FAILED", f"{label} rejected the credentials ({resp.status_code})")
    if resp.status_code == 429 or resp.status_code >= 502:
        return TransientNodeError("DATABASE_UNAVAILABLE", f"{label} returned {resp.status_code}")
    return NodeError("SQL_EXECUTION_ERROR", f"{label}: {resp.text[:500].strip()}")


_INT_TYPES = re.compile(r"^(INT|INTEGER|BIGINT|SMALLINT|TINYINT|LONG|SHORT|BYTE|INT64|U?INT\d+)$", re.I)
_FLOAT_TYPES = re.compile(r"^(DOUBLE|FLOAT|FLOAT64|NUMERIC|BIGNUMERIC|DECIMAL.*|REAL|FLOAT\d+)$", re.I)


def _typed(value: Any, type_name: str | None) -> Any:
    """String cells from JSON APIs → numbers/booleans where the column type says so."""
    if value is None or not isinstance(value, str) or not type_name:
        return value
    try:
        if _INT_TYPES.match(type_name):
            return int(value)
        if _FLOAT_TYPES.match(type_name):
            return float(value)
    except ValueError:
        return value
    if type_name.upper() in ("BOOLEAN", "BOOL"):
        return value.lower() == "true"
    return value


def _client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=httpx.Timeout(timeout + 15, connect=10))


def connect_databricks(ctx: NodeContext, h: DatabaseHandle) -> HttpConnection:
    """Databricks SQL warehouse via the SQL Statement Execution API (verified Nov 2026)."""
    import httpx

    host = _require(h.host, "the workspace host (e.g. dbc-1234.cloud.databricks.com)", "databricks")
    host = host.removeprefix("https://").rstrip("/")
    warehouse = _require(h.warehouse, "a SQL warehouse ID (or its HTTP path)", "databricks").rstrip("/").split("/")[-1]
    token = _require(_secret(ctx, h.token), "an access token secret", "databricks")
    client = _client(h.timeout_seconds)
    base = f"https://{host}/api/2.0/sql/statements"
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "AgenticMind"}

    def run(sql: str) -> tuple[list[str], list[tuple]]:
        deadline = time.monotonic() + h.timeout_seconds
        body: dict[str, Any] = {
            "statement": sql,
            "warehouse_id": warehouse,
            "wait_timeout": f"{max(5, min(50, int(h.timeout_seconds)))}s",
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
            "row_limit": h.max_rows + 1,
        }
        if h.catalog:
            body["catalog"] = h.catalog
        if h.default_schema:
            body["schema"] = h.default_schema
        try:
            resp = client.post(base, json=body, headers=headers)
            if resp.status_code != 200:
                raise _http_error("databricks", resp)
            data = resp.json()
            while data.get("status", {}).get("state") in ("PENDING", "RUNNING"):
                if time.monotonic() > deadline:
                    client.post(f"{base}/{data['statement_id']}/cancel", headers=headers)
                    raise NodeError("SQL_TIMEOUT", f"Query exceeded {h.timeout_seconds:g}s")
                time.sleep(1)
                resp = client.get(f"{base}/{data['statement_id']}", headers=headers)
                if resp.status_code != 200:
                    raise _http_error("databricks", resp)
                data = resp.json()
        except httpx.HTTPError as exc:
            raise TransientNodeError("DATABASE_UNAVAILABLE", f"Databricks: {exc}") from exc
        status = data.get("status", {})
        if status.get("state") != "SUCCEEDED":  # FAILED/CANCELED still answer HTTP 200
            message = (status.get("error") or {}).get("message") or status.get("state", "unknown state")
            raise NodeError("SQL_EXECUTION_ERROR", f"Databricks: {message}")
        cols = (data.get("manifest") or {}).get("schema", {}).get("columns", [])
        names = [c["name"] for c in cols]
        types = [c.get("type_name") for c in cols]
        rows = [
            tuple(_typed(v, t) for v, t in zip(r, types, strict=False))
            for r in (data.get("result") or {}).get("data_array") or []
        ]
        return names, rows

    return HttpConnection(run, client.close)


def connect_clickhouse(ctx: NodeContext, h: DatabaseHandle) -> HttpConnection:
    """ClickHouse over its HTTP interface; ``readonly=1`` makes the server refuse writes."""
    import httpx

    parts = parse_url(ctx.secret(h.dsn)) if h.dsn is not None else {}
    host = parts.get("host") or _require(h.host, "a host", "clickhouse")
    secure = h.ssl != "disable"
    port = parts.get("port") or h.port or (8443 if secure else 8123)
    url = f"{'https' if secure else 'http'}://{host}:{port}/"
    headers = {
        "X-ClickHouse-User": parts.get("user") or h.user or "default",
        "X-ClickHouse-Key": parts.get("password") or _secret(ctx, h.password) or "",
        "User-Agent": "AgenticMind",
    }
    database = parts.get("database") or h.database
    client = _client(h.timeout_seconds)

    def run(sql: str) -> tuple[list[str], list[tuple]]:
        params: dict[str, Any] = {
            "default_format": "JSONCompact",
            "output_format_json_quote_64bit_integers": 0,
            "max_execution_time": int(h.timeout_seconds),
            "max_result_rows": h.max_rows + 1,
            "result_overflow_mode": "break",
        }
        if database:
            params["database"] = database
        params["readonly"] = 1  # last: settings above are applied first, then writes and SET are refused
        try:
            resp = client.post(url, params=params, content=sql.encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise TransientNodeError("DATABASE_UNAVAILABLE", f"ClickHouse: {exc}") from exc
        if resp.status_code != 200:
            if "TIMEOUT_EXCEEDED" in resp.text:
                raise NodeError("SQL_TIMEOUT", f"Query exceeded {h.timeout_seconds:g}s")
            raise _http_error("clickhouse", resp)
        data = resp.json()
        return [m["name"] for m in data.get("meta", [])], [tuple(r) for r in data.get("data", [])]

    return HttpConnection(run, client.close)


_TOKENS: dict[str, tuple[str, float]] = {}
_TOKENS_LOCK = threading.Lock()
BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"


def _google_token(key: dict[str, Any], client: Any) -> str:
    """OAuth access token for a service account (signed JWT bearer grant), cached until near expiry."""
    email = key.get("client_email")
    with _TOKENS_LOCK:
        cached = _TOKENS.get(email or "")
        if cached and cached[1] - 60 > time.time():
            return cached[0]
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise _driver_missing("bigquery", "cryptography") from exc

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    token_uri = key.get("token_uri") or "https://oauth2.googleapis.com/token"
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": key.get("private_key_id")}
    claims = {"iss": email, "scope": BIGQUERY_SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600}
    signing_input = f"{b64(json.dumps(header).encode())}.{b64(json.dumps(claims).encode())}"
    private_key = serialization.load_pem_private_key(key["private_key"].encode(), password=None)
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    resp = client.post(
        token_uri,
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": f"{signing_input}.{b64(signature)}",
        },
    )
    if resp.status_code != 200:
        raise NodeError("DATABASE_AUTH_FAILED", f"Google rejected the service account ({resp.status_code})")
    data = resp.json()
    with _TOKENS_LOCK:
        _TOKENS[email or ""] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def connect_bigquery(ctx: NodeContext, h: DatabaseHandle) -> HttpConnection:
    """BigQuery via the REST ``jobs.query`` API with a service-account key (verified Nov 2026)."""
    import httpx

    raw = _require(_secret(ctx, h.service_account), "a service-account JSON key secret", "bigquery")
    try:
        key = json.loads(raw)
        _ = key["client_email"], key["private_key"]
    except (ValueError, KeyError, TypeError) as exc:
        raise NodeError("DATABASE_NOT_CONFIGURED", "The BigQuery secret must hold a service-account JSON key") from exc
    project = h.project or key.get("project_id")
    _require(project, "a project ID", "bigquery")
    client = _client(h.timeout_seconds)
    api = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}"

    def run(sql: str) -> tuple[list[str], list[tuple]]:
        deadline = time.monotonic() + h.timeout_seconds
        try:
            headers = {"Authorization": f"Bearer {_google_token(key, client)}"}
            body: dict[str, Any] = {
                "query": sql,
                "useLegacySql": False,
                "maxResults": h.max_rows + 1,
                "timeoutMs": int(min(h.timeout_seconds, 60) * 1000),
                "jobTimeoutMs": str(int(h.timeout_seconds * 1000)),
            }
            if h.database:
                body["defaultDataset"] = {"projectId": project, "datasetId": h.database}
            if h.location:
                body["location"] = h.location
            resp = client.post(f"{api}/queries", json=body, headers=headers)
            if resp.status_code != 200:
                raise _bigquery_error(resp)
            data = resp.json()
            while not data.get("jobComplete"):
                job = data["jobReference"]
                if time.monotonic() > deadline:
                    client.post(f"{api}/jobs/{job['jobId']}/cancel", params={"location": job.get("location")},
                                headers=headers)  # fmt: skip
                    raise NodeError("SQL_TIMEOUT", f"Query exceeded {h.timeout_seconds:g}s")
                params = {"timeoutMs": 10_000, "maxResults": h.max_rows + 1, "location": job.get("location")}
                resp = client.get(f"{api}/queries/{job['jobId']}", params=params, headers=headers)
                if resp.status_code != 200:
                    raise _bigquery_error(resp)
                data = resp.json()
        except httpx.HTTPError as exc:
            raise TransientNodeError("DATABASE_UNAVAILABLE", f"BigQuery: {exc}") from exc
        fields = (data.get("schema") or {}).get("fields", [])
        names = [f["name"] for f in fields]
        types = [f.get("type") for f in fields]
        rows = [
            tuple(_typed(c.get("v"), t) for c, t in zip(r.get("f", []), types, strict=False))
            for r in data.get("rows") or []
        ]
        return names, rows

    return HttpConnection(run, client.close)


def _bigquery_error(resp: Any) -> NodeError:
    try:
        message = resp.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = resp.text[:300]
    if resp.status_code in (401, 403):
        return NodeError("DATABASE_AUTH_FAILED", f"BigQuery: {message}")
    if resp.status_code == 429 or resp.status_code >= 500:
        return TransientNodeError("DATABASE_UNAVAILABLE", f"BigQuery: {message}")
    return NodeError("SQL_EXECUTION_ERROR", f"BigQuery: {message}")


# ---- introspection ------------------------------------------------------------------------------
def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def introspection_sql(h: DatabaseHandle) -> tuple[str, str]:
    """(tables SQL → schema, name, type) and (columns SQL → schema, table, column, type, nullable, pk)."""
    e = h.engine
    if e == "mysql":
        where = (
            f"= {_lit(h.database)}"
            if h.database
            else ("NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')")
        )
        return (
            f"SELECT table_schema, table_name, table_type FROM information_schema.tables WHERE table_schema {where}",
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, column_key "
            f"FROM information_schema.columns WHERE table_schema {where} "
            "ORDER BY table_schema, table_name, ordinal_position",
        )
    if e == "clickhouse":
        return (
            "SELECT database, name, engine FROM system.tables WHERE database = currentDatabase()",
            "SELECT database, table, name, type, startsWith(type, 'Nullable'), is_in_primary_key "
            "FROM system.columns WHERE database = currentDatabase() ORDER BY table, position",
        )
    if e == "bigquery":
        if not h.database:
            raise NodeError("DATABASE_NOT_CONFIGURED", "BigQuery needs a dataset to list tables")
        ds = f"`{h.project + '.' if h.project else ''}{h.database}`"
        return (
            f"SELECT table_schema, table_name, table_type FROM {ds}.INFORMATION_SCHEMA.TABLES",
            "SELECT table_schema, table_name, column_name, data_type, is_nullable, 'NO' "
            f"FROM {ds}.INFORMATION_SCHEMA.COLUMNS ORDER BY table_name, ordinal_position",
        )
    excluded = {
        "sqlserver": "('INFORMATION_SCHEMA', 'sys', 'guest')",
        "snowflake": "('INFORMATION_SCHEMA')",
        "databricks": "('information_schema')",
        "duckdb": "('information_schema', 'pg_catalog')",
    }[e]
    catalog = " AND table_catalog = current_database()" if e == "duckdb" else ""
    return (
        f"SELECT table_schema, table_name, table_type FROM information_schema.tables "
        f"WHERE table_schema NOT IN {excluded}{catalog}",
        "SELECT table_schema, table_name, column_name, data_type, is_nullable, 'NO' "
        f"FROM information_schema.columns WHERE table_schema NOT IN {excluded}{catalog} "
        "ORDER BY table_schema, table_name, ordinal_position",
    )


def primary_keys_sql(h: DatabaseHandle) -> str | None:
    """Standard information_schema primary keys (best effort; not every engine enforces or exposes them)."""
    if h.engine in ("sqlserver", "snowflake", "databricks", "duckdb"):
        return (
            "SELECT kcu.table_schema, kcu.table_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
            "AND tc.table_schema = kcu.table_schema WHERE tc.constraint_type = 'PRIMARY KEY'"
        )
    return None


def foreign_keys_sql(h: DatabaseHandle) -> str | None:
    if h.engine == "mysql":
        where = f"= {_lit(h.database)}" if h.database else "IS NOT NULL"
        return (
            "SELECT table_schema, table_name, column_name, referenced_table_schema, referenced_table_name, "
            "referenced_column_name FROM information_schema.key_column_usage "
            f"WHERE referenced_table_name IS NOT NULL AND table_schema {where}"
        )
    return None

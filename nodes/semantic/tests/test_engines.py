"""Database engines (ADR-0026): DuckDB for real; HTTP engines against mocked APIs; drivers via fakes."""

from __future__ import annotations

import base64
import importlib
import json

import httpx
import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import DatabaseHandle, NodeContext, NodeError, SecretRef
from agenticmind_nodes_semantic import engines
from agenticmind_nodes_semantic.policy import check_sql
from agenticmind_registry import default_registry
from agenticmind_runtime import Engine, InMemoryObserver, LocalArtifactStore, RunStatus, compile_plan

db_mod = importlib.import_module("agenticmind_nodes_semantic.db")
nodes_mod = importlib.import_module("agenticmind_nodes_semantic.nodes")


def ctx_with(secrets: dict[str, str], tmp_path, artifacts=None) -> NodeContext:
    return NodeContext(
        run_id="r", project_id="prj_eng", node_id="x", invocation_id="i", attempt=1, run_input={},
        allowed_secrets=frozenset(secrets), _secret_lookup=secrets.get, _storage_root=tmp_path,
        _artifacts=artifacts,
    )  # fmt: skip


# ---- DuckDB: a real database file ---------------------------------------------------------------
@pytest.fixture
def duck(tmp_path):
    import duckdb

    path = tmp_path / "shop.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR, country VARCHAR)")
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, status VARCHAR, total DOUBLE)")
    con.execute("INSERT INTO customers VALUES (1, 'Ada', 'UK'), (2, 'Grace', 'US'), (3, 'Linus', 'FI')")
    con.execute(
        "INSERT INTO orders SELECT i, 1 + i % 3, CASE WHEN i % 4 = 0 THEN 'returned' ELSE 'delivered' END, i * 1.5 "
        "FROM range(1, 41) t(i)"
    )
    con.close()
    store = LocalArtifactStore(tmp_path / "artifacts")
    info = store.put_bytes("prj_eng", "shop.duckdb", path.read_bytes())
    return store, info.id, tmp_path


def n(i, t, **c):
    return {"id": i, "type": f"agenticmind.semantic.{t}" if "." not in t else t, "version": "1.0.0", "config": c}


def d(i, s, t):
    (a, b), (c, e) = s.split("."), t.split(".")
    return {"id": i, "kind": "data", "source": {"node": a, "port": b}, "target": {"node": c, "port": e}}


async def test_duckdb_inspect_profile_and_query(duck):
    store, file_id, tmp = duck
    doc = load_project({
        "ir_version": "1.0", "project": {"id": "prj_eng", "name": "D"},
        "nodes": [
            n("db", "database", engine="duckdb", file=[file_id], max_rows=50),
            n("schema", "schema_inspector"), n("profile", "table_profiler", table="orders", top_values=2),
            n("sql", "sql_executor"), n("q", "agenticmind.core.manual_input", key="sql"),
            n("o1", "agenticmind.core.output", key="schema"), n("o2", "agenticmind.core.output", key="profile"),
            n("o3", "agenticmind.core.output", key="rows"), n("o4", "agenticmind.core.output", key="result"),
        ],
        "edges": [
            d("e1", "db.database", "schema.database"), d("e2", "db.database", "profile.database"),
            d("e3", "db.database", "sql.database"), d("e4", "q.value", "sql.sql"),
            d("e5", "schema.schema", "o1.value"), d("e6", "profile.profile", "o2.value"),
            d("e7", "sql.rows", "o3.value"), d("e8", "sql.result", "o4.value"),
        ],
    })  # fmt: skip
    result = await Engine(observer=InMemoryObserver(), artifacts=store, data_dir=tmp).run(
        compile_plan(doc, default_registry()),
        inputs={"sql": "select status, count(*) as n from orders group by status order by n desc"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    tables = {t["name"]: t for t in result.outputs["schema"]["tables"]}
    assert set(tables) == {"customers", "orders"} and tables["orders"]["row_count"] == 40
    assert next(c for c in tables["orders"]["columns"] if c["name"] == "id")["primary_key"] is True
    status = next(c for c in result.outputs["profile"]["columns"] if c["name"] == "status")
    assert status["distinct"] == 2 and status["top_values"][0] == {"value": "delivered", "count": 30}
    total = next(c for c in result.outputs["profile"]["columns"] if c["name"] == "total")
    assert total["max"] == 60.0
    assert result.outputs["rows"] == [{"status": "delivered", "n": 30}, {"status": "returned", "n": 10}]
    assert result.outputs["result"]["sql"].endswith("LIMIT 50")


async def test_duckdb_is_read_only_bounded_and_cannot_read_files(duck):
    store, file_id, tmp = duck
    ctx = ctx_with({}, tmp, store.scoped("prj_eng"))
    db = await db_mod.open_database(ctx, DatabaseHandle(engine="duckdb", artifact_id=file_id, timeout_seconds=0.5))
    try:
        with pytest.raises(NodeError) as exc:  # the connection itself refuses writes
            db.query("DELETE FROM orders")
        assert exc.value.code == "SQL_EXECUTION_ERROR" and "read-only" in exc.value.message.lower()
        with pytest.raises(NodeError) as exc:  # and other files are out of reach
            db.query("SELECT * FROM read_csv('shop.duckdb')")
        assert exc.value.code == "SQL_EXECUTION_ERROR"
        with pytest.raises(NodeError) as exc:
            db.query("SELECT count(*) FROM range(100000000000) a, range(1000) b")
        assert exc.value.code == "SQL_TIMEOUT"
    finally:
        db.close()
    bad = store.put_bytes("prj_eng", "not.duckdb", b"x" * 64).id
    with pytest.raises(NodeError) as exc:
        await db_mod.open_database(ctx, DatabaseHandle(engine="duckdb", artifact_id=bad))
    assert exc.value.code == "DATABASE_INVALID_FILE"


# ---- policy across dialects ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "dialect,sql,expected",
    [
        ("tsql", "SELECT name FROM customers", "SELECT TOP 100 name FROM customers"),
        ("mysql", "SELECT `name` FROM customers", "SELECT `name` FROM customers LIMIT 100"),
        ("snowflake", "SELECT NAME FROM CUSTOMERS", "SELECT NAME FROM CUSTOMERS LIMIT 100"),
        ("bigquery", "SELECT name FROM shop.customers", "SELECT name FROM shop.customers LIMIT 100"),
        ("databricks", "SELECT name FROM main.shop.customers", "SELECT name FROM main.shop.customers LIMIT 100"),
    ],
)
def test_policy_bounds_rows_in_each_dialect(dialect, sql, expected):
    known = {"customers", "shop.customers"}
    assert check_sql(sql, dialect=dialect, known_tables=known, max_rows=100).sql == expected


@pytest.mark.parametrize(
    "dialect,sql",
    [
        ("mysql", "SELECT SLEEP(10) FROM customers"),
        ("mysql", "SELECT LOAD_FILE('/etc/passwd') FROM customers"),
        ("tsql", "SELECT * FROM OPENROWSET('SQLNCLI', 'srv', 'SELECT 1')"),
        ("duckdb", "SELECT * FROM read_parquet('/data/*.parquet')"),
        ("clickhouse", "SELECT * FROM url('http://169.254.169.254/latest', CSV)"),
        ("clickhouse", "SELECT * FROM s3('https://bucket/x.csv')"),
    ],
)
def test_policy_denies_file_network_and_stall_functions(dialect, sql):
    with pytest.raises(NodeError) as exc:
        check_sql(sql, dialect=dialect, known_tables={"customers"}, max_rows=10)
    assert exc.value.code == "SQL_FUNCTION_DENIED"


# ---- ClickHouse (HTTP) --------------------------------------------------------------------------
def mock_http(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_clickhouse_over_http_is_readonly(monkeypatch, tmp_path):
    seen = []

    def handler(request):
        sql = request.content.decode()
        seen.append((request.url, dict(request.headers), sql))
        if "system.tables" in sql:
            return httpx.Response(
                200,
                json={
                    "meta": [{"name": "database"}, {"name": "name"}, {"name": "engine"}],
                    "data": [["shop", "events", "MergeTree"], ["shop", "v", "View"]],
                },
            )
        if "system.columns" in sql:
            return httpx.Response(
                200,
                json={
                    "meta": [],
                    "data": [
                        ["shop", "events", "id", "UInt64", 0, 1],
                        ["shop", "events", "note", "Nullable(String)", 1, 0],
                    ],
                },
            )
        if "sleep" in sql:
            return httpx.Response(500, text="Code: 159. DB::Exception: Timeout exceeded. (TIMEOUT_EXCEEDED)")
        return httpx.Response(200, json={"meta": [{"name": "n", "type": "UInt64"}], "data": [[42]], "rows": 1})

    # fmt: skip

    mock_http(monkeypatch, handler)
    h = DatabaseHandle(engine="clickhouse", host="ch.example.com", user="reader", password=SecretRef(name="CH"),
                       database="shop", max_rows=10, timeout_seconds=5)  # fmt: skip
    conn = engines.connect_clickhouse(ctx_with({"CH": "pw-fixture"}, tmp_path), h)
    db = db_mod.Database(h, conn)
    tables = {t.name: t for t in db.tables()}
    assert tables["v"].kind == "view" and tables["events"].columns[0]["primary_key"] is True
    assert tables["events"].columns[1]["nullable"] is True
    assert db.query("SELECT count() AS n FROM events").rows == [{"n": 42}]
    url, headers, _ = seen[-1]
    assert str(url).startswith("https://ch.example.com:8443/") and headers["x-clickhouse-key"] == "pw-fixture"
    params = list(url.params.items())
    assert params[-1] == ("readonly", "1") and ("max_execution_time", "5") in params
    assert ("max_result_rows", "11") in params and ("database", "shop") in params
    with pytest.raises(NodeError) as exc:
        db.query("SELECT sleep(3)")
    assert exc.value.code == "SQL_TIMEOUT"


# ---- Databricks (SQL Statement Execution API) ---------------------------------------------------
def test_databricks_polls_converts_types_and_reports_failures(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.headers.get("authorization"),
                      json.loads(request.content) if request.content else None))  # fmt: skip
        if request.method == "POST" and request.url.path.endswith("/statements"):
            body = json.loads(request.content)
            if "broken" in body["statement"]:
                return httpx.Response(
                    200,
                    json={
                        "statement_id": "s2",
                        "status": {"state": "FAILED", "error": {"message": "[TABLE_OR_VIEW_NOT_FOUND] broken"}},
                    },
                )
            return httpx.Response(200, json={"statement_id": "s1", "status": {"state": "PENDING"}})
        return httpx.Response(
            200,
            json={
                "statement_id": "s1",
                "status": {"state": "SUCCEEDED"},
                "manifest": {
                    "schema": {
                        "columns": [
                            {"name": "status", "type_name": "STRING"},
                            {"name": "n", "type_name": "LONG"},
                            {"name": "avg", "type_name": "DOUBLE"},
                            {"name": "ok", "type_name": "BOOLEAN"},
                        ]
                    }
                },
                "result": {"data_array": [["delivered", "30", "1.5", "true"], ["returned", "10", None, "false"]]},
            },
        )

    # fmt: skip

    mock_http(monkeypatch, handler)
    monkeypatch.setattr(engines.time, "sleep", lambda s: None)
    h = DatabaseHandle(engine="databricks", host="https://dbc-1.cloud.databricks.com/", token=SecretRef(name="DBX"),
                       warehouse="/sql/1.0/warehouses/abc123", catalog="main", default_schema="shop")  # fmt: skip
    db = db_mod.Database(h, engines.connect_databricks(ctx_with({"DBX": "dapi-fixture"}, tmp_path), h))
    rows = db.query("SELECT status, count(*) AS n FROM orders GROUP BY status").rows
    assert rows == [{"status": "delivered", "n": 30, "avg": 1.5, "ok": True},
                    {"status": "returned", "n": 10, "avg": None, "ok": False}]  # fmt: skip
    method, path, auth, body = calls[0]
    assert (method, path, auth) == ("POST", "/api/2.0/sql/statements", "Bearer dapi-fixture")
    assert body["warehouse_id"] == "abc123" and body["catalog"] == "main" and body["schema"] == "shop"
    assert body["disposition"] == "INLINE" and body["row_limit"] == 501 and calls[1][0] == "GET"
    with pytest.raises(NodeError) as exc:
        db.query("SELECT * FROM broken")
    assert exc.value.code == "SQL_EXECUTION_ERROR" and "TABLE_OR_VIEW_NOT_FOUND" in exc.value.message


# ---- BigQuery (REST + service-account JWT) -------------------------------------------------------
def test_bigquery_signs_a_token_and_reads_rows(monkeypatch, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    key = {
        "type": "service_account",
        "project_id": "acme-analytics",
        "private_key_id": "k1",
        "private_key": pem,
        "client_email": "reader@acme-analytics.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.test/token",
    }
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "oauth2.test":
            return httpx.Response(200, json={"access_token": "ya29.fixture", "expires_in": 3600})
        if request.method == "POST":
            return httpx.Response(200, json={"jobComplete": False, "jobReference": {"jobId": "j1", "location": "EU"}})
        return httpx.Response(
            200,
            json={
                "jobComplete": True,
                "schema": {"fields": [{"name": "country", "type": "STRING"}, {"name": "n", "type": "INTEGER"}]},
                "rows": [{"f": [{"v": "UK"}, {"v": "7"}]}],
                "totalRows": "1",
            },
        )

    # fmt: skip

    mock_http(monkeypatch, handler)
    engines._TOKENS.clear()
    h = DatabaseHandle(engine="bigquery", service_account=SecretRef(name="BQ"), database="shop")
    db = db_mod.Database(h, engines.connect_bigquery(ctx_with({"BQ": json.dumps(key)}, tmp_path), h))
    assert db.query("SELECT country, COUNT(*) AS n FROM customers GROUP BY 1").rows == [{"country": "UK", "n": 7}]
    token_req, query_req, poll_req = seen
    assertion = dict(x.split("=", 1) for x in token_req.content.decode().split("&"))["assertion"]
    claims = json.loads(base64.urlsafe_b64decode(assertion.split(".")[1] + "=="))
    assert claims["iss"] == key["client_email"] and claims["scope"].endswith("bigquery.readonly")
    body = json.loads(query_req.content)
    assert query_req.url.path == "/bigquery/v2/projects/acme-analytics/queries"
    assert body["useLegacySql"] is False and body["defaultDataset"] == {
        "projectId": "acme-analytics",
        "datasetId": "shop",
    }
    assert query_req.headers["authorization"] == "Bearer ya29.fixture"
    assert poll_req.url.path.endswith("/queries/j1") and poll_req.url.params["location"] == "EU"
    with pytest.raises(NodeError) as exc:
        engines.connect_bigquery(ctx_with({"BQ": "not json"}, tmp_path), h)
    assert exc.value.code == "DATABASE_NOT_CONFIGURED"


# ---- DB-API drivers: connection parameters and read-only sessions --------------------------------
class FakeCursor:
    def __init__(self, log):
        self.log = log
        self.description = None

    def execute(self, sql, params=()):
        self.log.append(sql)

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, **kw):
        self.kw = kw
        self.log = []

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        pass

    def close(self):
        pass


def test_mysql_parses_the_dsn_and_opens_a_read_only_session(monkeypatch, tmp_path):
    import pymysql

    made = []
    monkeypatch.setattr(pymysql, "connect", lambda **kw: made.append(FakeConn(**kw)) or made[-1])
    h = DatabaseHandle(engine="mysql", dsn=SecretRef(name="MY"), timeout_seconds=7)
    conn = engines.connect_mysql(ctx_with({"MY": "mysql://app:s%40cret@db.example.com:3307/shop"}, tmp_path), h)
    kw = conn.kw
    assert (kw["host"], kw["port"], kw["user"], kw["password"], kw["database"]) == (
        "db.example.com", 3307, "app", "s@cret", "shop")  # fmt: skip
    assert kw["ssl"] == {"check_hostname": False} and kw["autocommit"] is False
    assert conn.log[:2] == ["SET SESSION TRANSACTION READ ONLY", "SET SESSION max_execution_time = 7000"]
    h2 = DatabaseHandle(engine="mysql", host="h", user="u", password=SecretRef(name="PW"), ssl="disable")
    assert "ssl" not in engines.connect_mysql(ctx_with({"PW": "x"}, tmp_path), h2).kw


def test_sqlserver_and_snowflake_parameters(monkeypatch, tmp_path):
    import pymssql
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    monkeypatch.setattr(pymssql, "connect", lambda **kw: FakeConn(**kw))
    h = DatabaseHandle(engine="sqlserver", host="srv.database.windows.net", user="reader",
                       password=SecretRef(name="PW"), database="sales", timeout_seconds=9)  # fmt: skip
    kw = engines.connect_sqlserver(ctx_with({"PW": "x"}, tmp_path), h).kw
    assert (kw["server"], kw["port"], kw["database"], kw["timeout"], kw["autocommit"]) == (
        "srv.database.windows.net", "1433", "sales", 9, False)  # fmt: skip

    monkeypatch.setattr(snowflake.connector, "connect", lambda **kw: FakeConn(**kw))
    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        .decode()
    )
    h = DatabaseHandle(engine="snowflake", account="acme-prod", user="SVC_AGENT", private_key=SecretRef(name="PK"),
                       warehouse="WH", database="ANALYTICS", default_schema="PUBLIC", role="READER",
                       timeout_seconds=30)  # fmt: skip
    kw = engines.connect_snowflake(ctx_with({"PK": pem}, tmp_path), h).kw
    assert kw["account"] == "acme-prod" and kw["schema"] == "PUBLIC" and "password" not in kw
    assert isinstance(kw["private_key"], bytes) and kw["session_parameters"]["STATEMENT_TIMEOUT_IN_SECONDS"] == 30


def test_config_validation_and_dialect_names():
    cfg = nodes_mod.DatabaseConfig
    with pytest.raises(ValueError, match="databricks needs host, SQL warehouse and a token secret"):
        cfg(engine="databricks", host="h")
    with pytest.raises(ValueError, match="snowflake needs account and user"):
        cfg(engine="snowflake", password={"$secret": "P"})
    with pytest.raises(ValueError, match="a dsn secret, or host and user"):
        cfg(engine="mysql", host="h")
    assert (
        cfg(engine="postgresql", host="db.abc.supabase.co", user="postgres", password={"$secret": "SB"}).ssl
        == "require"
    )
    schema = nodes_mod.database.manifest.config_schema["properties"]
    assert schema["warehouse"]["x-ui"]["show_when"] == {"engine": ["snowflake", "databricks"]}
    assert engines.ENGINES["sqlserver"].dialect == "tsql" and engines.ENGINES["bigquery"].sql_name.startswith(
        "BigQuery"
    )
    assert engines.parse_url("postgresql://u:p%2Fq@h:6543/db")["password"] == "p/q"

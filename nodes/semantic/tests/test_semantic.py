"""Semantic data nodes: SQL policy, read-only connections, inspection, profiling, NL-to-SQL, agent tool."""

import importlib
import os

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import DatabaseHandle, ModelUsage, NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_semantic.policy import check_sql
from agenticmind_nodes_semantic.sample import build_sample_database
from agenticmind_providers import ChatResponse, ToolCall
from agenticmind_registry import default_registry
from agenticmind_runtime import Engine, InMemoryObserver, LocalArtifactStore, RunStatus, compile_plan

nodes_mod = importlib.import_module("agenticmind_nodes_semantic.nodes")
db_mod = importlib.import_module("agenticmind_nodes_semantic.db")
KNOWN = {"customers", "orders", "order_items", "products"}


@pytest.fixture
def sales(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    path = build_sample_database(tmp_path / "sales.sqlite")
    info = store.put_bytes("prj_sem", "sales.sqlite", path.read_bytes())
    return store, info.id, tmp_path


# ---- policy -------------------------------------------------------------------------------------
def test_policy_allows_reads_and_bounds_rows():
    ok = check_sql("select name from customers", dialect="sqlite", known_tables=KNOWN, max_rows=10)
    assert ok.sql.endswith("LIMIT 10") and ok.tables == ["customers"]
    own = check_sql("select name from customers limit 1", dialect="sqlite", known_tables=KNOWN, max_rows=10)
    assert own.capped is False and own.limit == 1  # the query's own LIMIT is not truncation
    assert ok.capped is True
    clamped = check_sql("SELECT * FROM orders LIMIT 5000;", dialect="sqlite", known_tables=KNOWN, max_rows=100)
    assert "LIMIT 100" in clamped.sql and clamped.notes
    cte = check_sql(
        "with big as (select customer_id, count(*) n from orders group by 1) "
        "select * from big join customers c on c.id = big.customer_id",
        dialect="sqlite",
        known_tables=KNOWN,
        max_rows=50,
    )
    assert set(cte.tables) == {"orders", "customers"}  # the CTE name is not a table
    union = check_sql(
        "select id from customers union select id from products", dialect="sqlite", known_tables=KNOWN, max_rows=7
    )
    assert union.sql.upper().startswith("SELECT * FROM (") and union.sql.endswith("LIMIT 7")


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("insert into customers (name) values ('x')", "SQL_NOT_READ_ONLY"),
        ("drop table orders", "SQL_NOT_READ_ONLY"),
        ("pragma table_info(orders)", "SQL_NOT_READ_ONLY"),
        ("select * into backup from orders", "SQL_NOT_READ_ONLY"),
        ("select 1; delete from orders", "SQL_MULTIPLE_STATEMENTS"),
        ("select pg_sleep(30)", "SQL_FUNCTION_DENIED"),
        ("select * from salaries", "SQL_UNKNOWN_TABLE"),
        ("select * from (", "SQL_PARSE_ERROR"),
        ("", "SQL_EMPTY"),
    ],
)
def test_policy_rejections(sql, code):
    with pytest.raises(NodeError) as exc:
        check_sql(sql, dialect="sqlite", known_tables=KNOWN, max_rows=10)
    assert exc.value.code == code


def test_policy_catches_data_modifying_ctes_and_allow_list():
    with pytest.raises(NodeError) as exc:
        check_sql(
            "with d as (delete from orders returning *) select * from d",
            dialect="postgres",
            known_tables=KNOWN,
            max_rows=5,
        )
    assert exc.value.code == "SQL_NOT_READ_ONLY"
    with pytest.raises(NodeError) as exc:
        check_sql("select * from customers", dialect="sqlite", known_tables=KNOWN, max_rows=5, allowed={"orders"})
    assert exc.value.code == "SQL_TABLE_NOT_ALLOWED"


# ---- through the engine -------------------------------------------------------------------------
def n(i, t, **c):
    return {"id": i, "type": f"agenticmind.semantic.{t}" if "." not in t else t, "version": "1.0.0", "config": c}


def d(i, s, t):
    (a, b), (c, e) = s.split("."), t.split(".")
    return {"id": i, "kind": "data", "source": {"node": a, "port": b}, "target": {"node": c, "port": e}}


async def run_graph(sales, nodes, edges, inputs=None):
    store, _, tmp = sales
    doc = load_project({"ir_version": "1.0", "project": {"id": "prj_sem", "name": "S"}, "nodes": nodes, "edges": edges})
    result = await Engine(observer=InMemoryObserver(), artifacts=store, data_dir=tmp).run(
        compile_plan(doc, default_registry()), inputs=inputs or {}
    )
    return result


async def test_inspect_profile_and_execute(sales):
    _, file_id, _ = sales
    result = await run_graph(
        sales,
        [
            n("db", "database", file=[file_id], max_rows=100),
            n("schema", "schema_inspector", sample_rows=1),
            n("profile", "table_profiler", table="orders", top_values=3),
            n("sql", "sql_executor"),
            n("q", "agenticmind.core.manual_input", key="sql"),
            n("o1", "agenticmind.core.output", key="schema"),
            n("o2", "agenticmind.core.output", key="text"),
            n("o3", "agenticmind.core.output", key="profile"),
            n("o4", "agenticmind.core.output", key="rows"),
            n("o5", "agenticmind.core.output", key="result"),
        ],
        [
            d("e1", "db.database", "schema.database"),
            d("e2", "db.database", "profile.database"),
            d("e3", "db.database", "sql.database"),
            d("e4", "q.value", "sql.sql"),
            d("e5", "schema.schema", "o1.value"),
            d("e6", "schema.text", "o2.value"),
            d("e7", "profile.profile", "o3.value"),
            d("e8", "sql.rows", "o4.value"),
            d("e9", "sql.result", "o5.value"),
        ],
        {"sql": "select status, count(*) as n from orders group by status order by n desc"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    tables = {t["name"]: t for t in result.outputs["schema"]["tables"]}
    assert set(tables) == KNOWN and tables["orders"]["row_count"] == 600 and len(tables["orders"]["sample_rows"]) == 1
    assert {"columns": ["customer_id"], "ref_table": "customers", "ref_columns": ["id"]} in tables["orders"][
        "foreign_keys"
    ]
    assert "customer_id INTEGER -> customers.id" in result.outputs["text"]
    status = next(c for c in result.outputs["profile"]["columns"] if c["name"] == "status")
    assert status["null_rate"] == 0 and status["distinct"] == 4 and status["top_values"][0]["value"] == "delivered"
    assert sum(r["n"] for r in result.outputs["rows"]) == 600 and result.outputs["result"]["sql"].endswith("LIMIT 100")


async def test_connection_is_read_only_even_without_the_policy(sales):
    store, file_id, tmp = sales
    from agenticmind_node_sdk import NodeContext

    ctx = NodeContext(
        run_id="r",
        project_id="prj_sem",
        node_id="x",
        invocation_id="i",
        attempt=1,
        run_input={},
        _artifacts=store.scoped("prj_sem"),
        _storage_root=tmp,
    )
    db = await db_mod.open_database(ctx, DatabaseHandle(engine="sqlite", artifact_id=file_id, timeout_seconds=0.5))
    try:
        with pytest.raises(NodeError) as exc:
            db.query("DELETE FROM orders")  # bypassing check_sql on purpose
        assert exc.value.code == "SQL_EXECUTION_ERROR" and "readonly" in exc.value.message.replace("-", "").lower()
        assert db.query("SELECT COUNT(*) AS n FROM orders").rows[0]["n"] == 600
        with pytest.raises(NodeError) as exc:  # runaway query hits the timeout
            db.query("WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) SELECT COUNT(*) FROM r")
        assert exc.value.code == "SQL_TIMEOUT"
    finally:
        db.close()


async def test_not_a_sqlite_file_and_missing_config(sales):
    store, _, _ = sales
    bad = store.put_bytes("prj_sem", "notes.db", b"just text").id
    result = await run_graph(
        sales,
        [n("db", "database", file=[bad]), n("s", "schema_inspector"), n("o", "agenticmind.core.output")],
        [d("e1", "db.database", "s.database"), d("e2", "s.text", "o.value")],
    )
    assert result.status == RunStatus.FAILED and result.error.code == "DATABASE_INVALID_FILE"
    with pytest.raises(NodeError) as exc:
        await invoke_node(nodes_mod.database, config={"engine": "sqlite"})
    assert exc.value.code == "DATABASE_NOT_CONFIGURED"


class Scripted:
    def __init__(self, *sqls):
        self.sqls, self.requests = list(sqls), []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        sql = self.sqls.pop(0)
        return ChatResponse(
            text="",
            tool_calls=[
                ToolCall(
                    id="1",
                    name="submit_sql",
                    arguments={"answerable": True, "sql": sql, "explanation": "Revenue by category."},
                )
            ],
            usage=ModelUsage(provider="x", model="m", input_tokens=50, output_tokens=10, estimated_cost_usd=0.0001),
        )


async def test_nl_to_sql_retries_with_the_error(sales, monkeypatch):
    _, file_id, _ = sales
    prov = Scripted(
        "select category, sum(quantity * price) from sales",  # unknown table
        "select p.category, round(sum(oi.quantity * oi.unit_price), 2) as revenue from order_items oi "
        "join products p on p.id = oi.product_id group by p.category order by revenue desc",
    )
    monkeypatch.setattr(nodes_mod, "get_provider", lambda name: prov)
    result = await run_graph(
        sales,
        [
            n("db", "database", file=[file_id]),
            n("q", "agenticmind.core.manual_input", key="question"),
            n("nl", "nl_to_sql", provider="mock", model="m"),
            n("o", "agenticmind.core.output", key="answer"),
        ],
        [d("e1", "db.database", "nl.database"), d("e2", "q.value", "nl.question"), d("e3", "nl.result", "o.value")],
        {"question": "Revenue by product category?"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    answer = result.outputs["answer"]
    assert [a["error"] is None for a in answer["attempts"]] == [False, True]
    assert "SQL_UNKNOWN_TABLE" in answer["attempts"][0]["error"]
    assert answer["columns"] == ["category", "revenue"] and answer["row_count"] == 5 and "5 rows" in answer["answer"]
    assert "Earlier attempts failed" in prov.requests[1].messages[0].content
    assert "TABLE order_items" in prov.requests[0].system and prov.requests[0].tool_choice == "required"


async def test_agent_queries_through_the_sql_tool(sales):
    _, file_id, _ = sales
    result = await run_graph(
        sales,
        [
            n("db", "database", file=[file_id]),
            n("tool", "tool_sql"),
            n("q", "agenticmind.core.manual_input", key="question"),
            {
                "id": "agent",
                "type": "agenticmind.agents.agent",
                "version": "1.1.0",
                "config": {"provider": "mock", "model": "m"},
            },
            n("o", "agenticmind.core.output", key="answer"),
        ],
        [
            d("e1", "db.database", "tool.database"),
            d("e2", "tool.tool", "agent.tools"),
            d("e3", "q.value", "agent.task"),
            d("e4", "agent.result", "o.value"),
        ],
        {"question": "query_database: select count(*) as n from customers"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    step = result.outputs["answer"]["steps"][0]
    assert step["tool"] == "query_database" and step["ok"] and '"n": 40' in step["result_summary"]
    blocked = await run_graph(
        sales,
        [
            n("db", "database", file=[file_id]),
            n("tool", "tool_sql"),
            n("q", "agenticmind.core.manual_input", key="question"),
            {
                "id": "agent",
                "type": "agenticmind.agents.agent",
                "version": "1.1.0",
                "config": {"provider": "mock", "model": "m"},
            },
            n("o", "agenticmind.core.output", key="answer"),
        ],
        [
            d("e1", "db.database", "tool.database"),
            d("e2", "tool.tool", "agent.tools"),
            d("e3", "q.value", "agent.task"),
            d("e4", "agent.result", "o.value"),
        ],
        {"question": "query_database: delete from customers"},
    )
    assert "SQL_NOT_READ_ONLY" in blocked.outputs["answer"]["steps"][0]["result_summary"]


def test_sample_database_is_deterministic(tmp_path):
    a = build_sample_database(tmp_path / "a.sqlite").read_bytes()
    b = build_sample_database(tmp_path / "b.sqlite").read_bytes()
    assert a == b


@pytest.mark.skipif(
    not os.environ.get("AGENTICMIND_TEST_POSTGRES_DSN"), reason="set AGENTICMIND_TEST_POSTGRES_DSN to run"
)
async def test_postgres_read_only(tmp_path):  # pragma: no cover - opt-in
    from agenticmind_node_sdk import NodeContext, SecretRef

    ctx = NodeContext(
        run_id="r",
        project_id="p",
        node_id="x",
        invocation_id="i",
        attempt=1,
        run_input={},
        allowed_secrets=frozenset({"PG"}),
        _secret_lookup={"PG": os.environ["AGENTICMIND_TEST_POSTGRES_DSN"]}.get,
        _storage_root=tmp_path,
    )
    db = await db_mod.open_database(ctx, DatabaseHandle(engine="postgresql", dsn=SecretRef(name="PG")))
    try:
        assert isinstance(db.tables(), list)
        with pytest.raises(NodeError):
            db.query("CREATE TABLE agenticmind_should_fail (id int)")
    finally:
        db.close()


async def test_nl_to_sql_refuses_writes_instead_of_substituting(sales, monkeypatch):
    _, file_id, _ = sales

    class Writer:
        def __init__(self):
            self.requests = []

        async def chat(self, request, *, api_key=None, base_url=None):
            self.requests.append(request)
            args = (
                {"answerable": True, "sql": "DELETE FROM orders WHERE status = 'cancelled'", "explanation": "Delete."}
                if len(self.requests) == 1
                else {
                    "answerable": False,
                    "sql": "",
                    "explanation": "The database is read-only; orders cannot be deleted.",
                }
            )
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(id="1", name="submit_sql", arguments=args)],
                usage=ModelUsage(provider="x", model="m"),
            )

    prov = Writer()
    monkeypatch.setattr(nodes_mod, "get_provider", lambda name: prov)
    result = await run_graph(
        sales,
        [
            n("db", "database", file=[file_id]),
            n("q", "agenticmind.core.manual_input", key="question"),
            n("nl", "nl_to_sql", provider="mock", model="m"),
            n("o", "agenticmind.core.output", key="answer"),
        ],
        [d("e1", "db.database", "nl.database"), d("e2", "q.value", "nl.question"), d("e3", "nl.result", "o.value")],
        {"question": "Delete all cancelled orders"},
    )
    answer = result.outputs["answer"]
    assert result.status == RunStatus.SUCCEEDED and answer["answerable"] is False and answer["sql"] == ""
    assert "read-only" in answer["answer"] and "SQL_NOT_READ_ONLY" in answer["attempts"][0]["error"]
    assert "do not substitute" in prov.requests[1].messages[0].content

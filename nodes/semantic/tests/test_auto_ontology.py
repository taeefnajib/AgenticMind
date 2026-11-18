"""Auto Ontology adapter (ADR-0019): contract tests against the pinned OpenAPI subset, node behaviour,
capability checks and the agent tool. A fake gateway rejects any request the pinned spec does not
declare, and the fake's own fixture responses are validated against the spec's response schemas."""

from __future__ import annotations

import importlib
import json
import os
import re
from importlib.resources import files
from typing import Any

import httpx
import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import NodeError
from agenticmind_nodes_semantic.auto_ontology import API_VERSION, UPSTREAM_COMMIT, AutoOntologyClient, parse_rows
from agenticmind_registry import default_registry
from agenticmind_runtime import Engine, InMemoryObserver, MappingSecretResolver, RunStatus, compile_plan
from jsonschema import Draft202012Validator

onto_mod = importlib.import_module("agenticmind_nodes_semantic.ontology_nodes")
SPEC = json.loads(
    files("agenticmind_nodes_semantic.contract").joinpath("auto-ontology-api.subset.json").read_text("utf-8")
)
TOKEN = "fixture-ao-token-123"


# ---- contract: the vendored subset is the pinned one --------------------------------------------
def test_contract_is_pinned():
    assert SPEC["info"]["version"] == API_VERSION
    assert SPEC["info"]["x-agenticmind-source"]["commit"] == UPSTREAM_COMMIT
    assert set(SPEC["paths"]) >= {"/api/health", "/api/terms", "/api/chat/completions"}


def _validate(schema: dict[str, Any], value: Any) -> None:
    Draft202012Validator({**schema, "components": SPEC["components"]}).validate(value)


def _operation(method: str, path: str) -> tuple[str, dict[str, Any]]:
    for template, ops in SPEC["paths"].items():
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$"
        if re.match(pattern, path) and method.lower() in ops:
            return template, ops[method.lower()]
    raise AssertionError(f"{method} {path} is not in the pinned Auto Ontology contract")


# ---- a fake gateway that enforces the contract --------------------------------------------------
TERMS = [
    {"id": "t_rev", "name": "Revenue", "synonyms": ["sales"], "description": "Quantity times unit price.", "tags": []},
    {"id": "t_cust", "name": "Customer", "synonyms": ["client"], "description": "A buyer.", "tags": []},
    {"id": "t_cat", "name": "Product Category", "synonyms": None, "description": "Grouping of products.", "tags": []},
]
DETAIL = {
    "t_rev": {
        "id": "t_rev",
        "name": "Revenue",
        "synonyms": ["sales"],
        "description": "Quantity times unit price.",
        "tables": [{"id": "tb1", "name": "order_items"}],
        "related_terms": [{"id": "t_cat", "name": "Product Category"}],
        "tags": [],
    },
}
DETAIL.update({t["id"]: {**t, "tables": [], "related_terms": []} for t in TERMS if t["id"] not in DETAIL})
SQL_ATTRS = {"t_rev": [{"id": "a1", "name": "total_revenue", "expression": "SUM(quantity * unit_price)"}]}


class FakeGateway:
    def __init__(self, *, calculated: bool = True, token: str = TOKEN, stream: list[dict] | None = None):
        self.calculated, self.token, self.requests = calculated, token, []
        self.stream = (
            stream
            if stream is not None
            else [
                {"type": "step", "node": "resolve_terms", "phase": "start"},
                {"type": "step", "node": "resolve_terms", "phase": "end", "thought": "Revenue, Product Category"},
                {"type": "sql", "sql": "SELECT ..."},
                {
                    "type": "result",
                    "answer": {
                        "response": "Office had the most revenue in 2025 (88,792.70).",
                        "sql_code": "SELECT category, SUM(quantity*unit_price) AS revenue FROM ...",
                        "sql_response_from_db": [json.dumps([{"category": "Office", "revenue": 88792.7}])],
                        "thoughts": "…",
                    },
                },
            ]
        )

    def _json(self, template: str, op: dict, status: int, body: Any) -> httpx.Response:
        schema = op["responses"][str(status)].get("content", {}).get("application/json", {}).get("schema")
        if schema is not None:
            _validate(schema, body)  # fixtures must honour the pinned response schema
        return httpx.Response(status, json=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        template, op = _operation(request.method, request.url.path)
        declared = {p["name"] for p in op.get("parameters") or [] if p["in"] == "query"}
        assert set(request.url.params) <= declared, f"undeclared query params for {template}"
        for p in op.get("parameters") or []:
            if p["in"] == "query" and p["name"] in request.url.params:
                value: Any = request.url.params[p["name"]]
                if "integer" in json.dumps(p["schema"]):
                    value = int(value)
                _validate(p["schema"], value)
        if op.get("requestBody"):
            _validate(op["requestBody"]["content"]["application/json"]["schema"], json.loads(request.content))
        if template == "/api/health":
            return self._json(template, op, 200, {"status": "ok", "postgres": {"status": "ok"}})
        if request.headers.get("x-api-key") != self.token:
            return httpx.Response(401)
        if template == "/api/semantic-compilation/status":
            body = {"calculated": self.calculated, "running": False, "last_success_at": None, "last_failure_at": None}
            return self._json(template, op, 200, body)
        if template == "/api/terms":
            q = request.url.params.get("query", "").lower()
            terms = [t for t in TERMS if q in t["name"].lower()]
            limit = int(request.url.params.get("limit", len(terms) or 1))
            page = {
                "terms": terms[:limit],
                "total": len(terms),
                "column_attribute_counts": [],
                "sql_attribute_counts": [],
                "related_counts": [],
            }
            return self._json(template, op, 200, page)
        term_id = request.url.path.split("/")[3]
        if template == "/api/terms/{term_id}":
            if term_id not in DETAIL:
                return httpx.Response(404, json={"detail": "Term not found"})
            return self._json(template, op, 200, {"data": DETAIL[term_id]})
        if template == "/api/terms/{term_id}/sql-attributes":
            attrs = SQL_ATTRS.get(term_id, [])
            return self._json(template, op, 200, {"data": attrs, "count": len(attrs), "total": len(attrs)})
        if template == "/api/chat/completions":
            if not self.calculated:
                return httpx.Response(409, json={"detail": "Semantic layer has not been compiled"})
            body = "".join(f"data: {json.dumps(e)}\n\n" for e in self.stream) + "data: [DONE]\n\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        raise AssertionError(template)


@pytest.fixture
def gateway(monkeypatch):
    gw = FakeGateway()

    def make(base_url, token, *, timeout=30, transport=None):
        return AutoOntologyClient(base_url, token, timeout=timeout, transport=httpx.MockTransport(gw))

    monkeypatch.setattr(onto_mod, "AutoOntologyClient", make)
    return gw


# ---- client -------------------------------------------------------------------------------------
async def test_client_requests_match_contract(gateway):
    async with AutoOntologyClient("http://ao.test", TOKEN, transport=httpx.MockTransport(gateway)) as c:
        assert (await c.health())["status"] == "ok"
        assert (await c.semantic_status())["calculated"] is True
        assert (await c.list_terms("rev", 500))["total"] == 1  # limit clamped to the spec's maximum (100)
        assert (await c.get_term("t_rev"))["tables"][0]["name"] == "order_items"
        assert (await c.term_sql_attributes("t_rev"))[0]["name"] == "total_revenue"
        steps = []

        async def on_step(s):
            steps.append(s)

        ans = await c.ask("Which category had the most revenue in 2025?", on_step=on_step)
    assert ans.rows == [{"category": "Office", "revenue": 88792.7}] and ans.columns == ["category", "revenue"]
    assert ans.sql.startswith("SELECT") and [s["phase"] for s in steps] == ["start", "end"]
    assert gateway.requests[2].url.params["limit"] == "100"
    assert all(r.headers.get("x-api-key") == TOKEN for r in gateway.requests[1:])


@pytest.mark.parametrize(
    ("raw", "rows"),
    [
        (['[{"count": 42}]'], [{"count": 42}]),
        ([{"a": 1}, {"a": 2}], [{"a": 1}, {"a": 2}]),
        ({"a": 1}, [{"a": 1}]),
        (None, []),
        (["not json"], []),
    ],
)
def test_parse_rows_matches_upstream_shapes(raw, rows):
    assert parse_rows(raw) == rows


async def test_client_errors_are_actionable(gateway):
    async with AutoOntologyClient("http://ao.test", "wrong", transport=httpx.MockTransport(gateway)) as c:
        with pytest.raises(NodeError) as exc:
            await c.semantic_status()
    assert exc.value.code == "ONTOLOGY_UNAUTHORIZED" and "API Tokens" in exc.value.message
    gateway.calculated = False
    async with AutoOntologyClient("http://ao.test", TOKEN, transport=httpx.MockTransport(gateway)) as c:
        with pytest.raises(NodeError) as exc:
            await c.ask("anything")
    assert exc.value.code == "ONTOLOGY_NOT_READY" and "Semantic Compilation" in exc.value.message
    gateway.calculated, gateway.stream = True, [{"type": "error", "message": "model endpoint failed"}]
    async with AutoOntologyClient("http://ao.test", TOKEN, transport=httpx.MockTransport(gateway)) as c:
        with pytest.raises(NodeError) as exc:
            await c.ask("anything")
    assert exc.value.code == "ONTOLOGY_ANSWER_FAILED" and "model endpoint failed" in exc.value.message
    gateway.stream = [{"type": "step", "node": "x", "phase": "end"}]
    async with AutoOntologyClient("http://ao.test", TOKEN, transport=httpx.MockTransport(gateway)) as c:
        with pytest.raises(NodeError) as exc:
            await c.ask("anything")
    assert exc.value.code == "ONTOLOGY_ANSWER_FAILED"


async def test_ask_has_a_total_deadline_even_while_steps_stream():
    # found live: upstream kept streaming step events while its embedding endpoint hung
    import asyncio

    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b'data: {"type": "step", "node": "retrieve", "phase": "end"}\n\n'
                await asyncio.sleep(0.02)

    def handler(request):
        return httpx.Response(200, stream=Endless(), headers={"content-type": "text/event-stream"})

    async with AutoOntologyClient("http://ao.test", TOKEN, transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(NodeError) as exc:
            await c.ask("anything", timeout=0.3)
    assert exc.value.code == "ONTOLOGY_TIMEOUT"


async def test_not_a_gateway_is_detected():
    def other(request):
        return httpx.Response(200, text="<html>hello</html>")

    async with AutoOntologyClient("http://localhost:9", None, transport=httpx.MockTransport(other)) as c:
        with pytest.raises(NodeError) as exc:
            await c.health()
    assert exc.value.code == "ONTOLOGY_API_ERROR" and "APP_URL" in exc.value.message


# ---- nodes through the engine -------------------------------------------------------------------
def n(i, t, **c):
    return {"id": i, "type": t if "." in t else f"agenticmind.semantic.{t}", "version": "1.0.0", "config": c}


def d(i, s, t):
    (a, b), (c, e) = s.split("."), t.split(".")
    return {"id": i, "kind": "data", "source": {"node": a, "port": b}, "target": {"node": c, "port": e}}


CONN = n("ao", "auto_ontology", base_url="http://ao.test", api_token={"$secret": "AO_TOKEN"})


async def run(nodes, edges, inputs=None, secrets=None):
    doc = load_project({"ir_version": "1.0", "project": {"id": "prj_ao", "name": "A"}, "nodes": nodes, "edges": edges})
    secrets = secrets or MappingSecretResolver({"AO_TOKEN": TOKEN})
    obs = InMemoryObserver()
    result = await Engine(observer=obs, secrets=secrets).run(
        compile_plan(doc, default_registry(), secrets=secrets), inputs=inputs or {}
    )
    return result, obs


def out(i, key):
    return n(i, "agenticmind.core.output", key=key)


async def test_connection_reports_capabilities_and_gates_readiness(gateway):
    result, obs = await run([CONN, out("o", "status")], [d("e", "ao.status", "o.value")])
    status = result.outputs["status"]
    assert result.status == RunStatus.SUCCEEDED
    assert status["authenticated"] and status["semantic_layer_ready"] and status["capabilities"]["ask_question"]
    assert status["pinned"] == {"api_version": API_VERSION, "commit": UPSTREAM_COMMIT}
    assert TOKEN not in json.dumps([e.model_dump(mode="json") for e in obs.events])  # never in traces

    gateway.calculated = False
    result, _ = await run([CONN, out("o", "status")], [d("e", "ao.status", "o.value")])
    assert result.status == RunStatus.FAILED and result.error.code == "ONTOLOGY_NOT_READY"
    lenient = {**CONN, "config": {**CONN["config"], "require_ready": False}}
    result, _ = await run([lenient, out("o", "status")], [d("e", "ao.status", "o.value")])
    assert result.status == RunStatus.SUCCEEDED and result.outputs["status"]["capabilities"]["ask_question"] is False


async def test_connection_with_wrong_token_fails_clearly(gateway):
    result, _ = await run(
        [CONN, out("o", "status")], [d("e", "ao.status", "o.value")], secrets=MappingSecretResolver({"AO_TOKEN": "x"})
    )
    assert result.status == RunStatus.FAILED and result.error.code == "ONTOLOGY_UNAUTHORIZED"


async def test_terms_details_context_and_ask(gateway):
    result, obs = await run(
        [
            CONN,
            n("q", "agenticmind.core.manual_input", key="question"),
            n("search", "ontology_terms", query="cust"),
            n("term", "ontology_term", term="revenue"),
            n("ctx", "ontology_context"),
            n("ask", "ontology_ask"),
            out("o1", "terms"),
            out("o2", "term"),
            out("o3", "context"),
            out("o4", "answer"),
        ],
        [
            d("e1", "ao.ontology", "search.ontology"),
            d("e2", "ao.ontology", "term.ontology"),
            d("e3", "ao.ontology", "ctx.ontology"),
            d("e4", "ao.ontology", "ask.ontology"),
            d("e5", "q.value", "ctx.question"),
            d("e6", "q.value", "ask.question"),
            d("e7", "search.text", "o1.value"),
            d("e8", "term.text", "o2.value"),
            d("e9", "ctx.context", "o3.value"),
            d("e10", "ask.result", "o4.value"),
        ],
        {"question": "Which product category had the most revenue in 2025?"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    o = result.outputs
    assert o["terms"].startswith("- Customer (also: client)")
    assert "tables: order_items" in o["term"] and "measure total_revenue: SUM(quantity * unit_price)" in o["term"]
    assert "Revenue" in o["context"] and "total_revenue" in o["context"]
    answer = o["answer"]
    assert answer["source"] == "auto_ontology" and answer["rows"] == [{"category": "Office", "revenue": 88792.7}]
    assert answer["columns"] == ["category", "revenue"] and answer["sql"].startswith("SELECT")
    steps = [e for e in obs.events if getattr(e, "data", None) and e.data.get("type") == "ontology_step"]
    assert steps and steps[0].data["node"] == "resolve_terms"


def test_ask_node_outlives_the_default_node_timeout():
    # found live: upstream runs take minutes, so the engine's 30 s default must not cut them off
    manifest = default_registry().latest("agenticmind.semantic.ontology_ask").manifest
    assert manifest.execution.timeout_seconds >= 1800 and manifest.execution.max_attempts == 1


def test_match_terms_is_deterministic():
    picked = onto_mod.match_terms("Total sales per product category for clients", TERMS, 5)
    assert [t["id"] for t in picked] == ["t_cat", "t_rev", "t_cust"]
    assert onto_mod.match_terms("weather tomorrow", TERMS, 5) == []


async def test_agent_uses_ontology_tool(gateway):
    # the mock provider calls the named tool with its required arguments (action = first enum value)
    agent = {
        "id": "agent",
        "type": "agenticmind.agents.agent",
        "version": "1.1.0",
        "config": {"provider": "mock", "model": "m"},
    }
    result, _ = await run(
        [
            CONN,
            n("tool", "tool_ontology"),
            agent,
            n("q", "agenticmind.core.manual_input", key="question"),
            out("o", "answer"),
        ],
        [
            d("e1", "ao.ontology", "tool.ontology"),
            d("e2", "tool.tool", "agent.tools"),
            d("e3", "q.value", "agent.task"),
            d("e4", "agent.result", "o.value"),
        ],
        {"question": "business_data: which terms exist?"},
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    step = result.outputs["answer"]["steps"][0]
    assert step["tool"] == "business_data" and step["ok"] and "Revenue" in step["result_summary"]


@pytest.mark.skipif(
    not (
        os.environ.get("AGENTICMIND_TEST_AUTO_ONTOLOGY_URL") and os.environ.get("AGENTICMIND_TEST_AUTO_ONTOLOGY_TOKEN")
    ),
    reason="live Auto Ontology test is opt-in (set AGENTICMIND_TEST_AUTO_ONTOLOGY_URL and _TOKEN)",
)
async def test_live_auto_ontology():  # pragma: no cover - opt-in
    url, token = os.environ["AGENTICMIND_TEST_AUTO_ONTOLOGY_URL"], os.environ["AGENTICMIND_TEST_AUTO_ONTOLOGY_TOKEN"]
    async with AutoOntologyClient(url, token) as c:
        assert (await c.health())["status"] == "ok"
        status = await c.semantic_status()
        page = await c.list_terms(limit=5)
        for template, value in (
            ("/api/semantic-compilation/status", status),
            ("/api/terms", page),
        ):
            schema = SPEC["paths"][template]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
            _validate(schema, value)  # the live deployment still honours the pinned contract

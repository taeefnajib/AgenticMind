"""Loops (for_each / repeat / nested / parallel), Router and Join through the real engine."""

import importlib
import time

from agenticmind_runtime import Code, Engine, InMemoryObserver, NodeStatus, RunStatus, compile_plan, validate_document
from conftest import c, d, doc, n

LOOP = "agenticmind.core.loop"
END = "agenticmind.core.loop_end"


async def run(registry, document, inputs=None):
    obs = InMemoryObserver()
    result = await Engine(observer=obs, retry_backoff_seconds=0.01).run(
        compile_plan(document, registry), inputs=inputs or {}
    )
    return result, obs


def codes(report):
    return {x.code for x in report.diagnostics}


def for_each_doc(body_nodes, body_edges, *, loop_cfg=None, value_src="t.text", items_type="JSON"):
    return doc(
        [
            n("items", "manual_input", key="items", value_type=items_type),
            n("loop", LOOP, **{"mode": "for_each", "item_type": "String", **(loop_cfg or {})}),
            *body_nodes,
            n("end", END),
            n("out", "output", key="results"),
        ],
        [
            d("e1", "items.value", "loop.items"),
            d("e2", "loop.scope", "end.scope"),
            *body_edges,
            d("e9", value_src, "end.value"),
            d("e10", "end.results", "out.value"),
        ],
    )


async def test_for_each_runs_body_per_item(registry):
    document = for_each_doc([n("t", "text_transform", operation="uppercase")], [d("e3", "loop.item", "t.text")])
    plan = compile_plan(document, registry)
    assert "t" not in plan.order and plan.regions["end"].order == ("t",)
    result, obs = await run(registry, document, {"items": ["a", "b", "c"]})
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs == {"results": ["A", "B", "C"]}
    iters = [e.data["iteration"] for e in obs.events if e.node_id == "t" and e.event_type.value == "node_succeeded"]
    assert sorted(iters) == [[0], [1], [2]]
    end_done = next(e for e in obs.events if e.node_id == "end" and e.event_type.value == "node_succeeded")
    assert end_done.data["loop"]["iterations"] == 3 and end_done.data["loop"]["stop_reason"] == "items_exhausted"


async def test_for_each_concurrency_keeps_order(registry):
    document = for_each_doc(
        [n("wait", "delay", seconds=0.25)],
        [d("e3", "loop.item", "wait.value")],
        loop_cfg={"concurrency": 4},
        value_src="wait.value",
    )
    t0 = time.perf_counter()
    result, _ = await run(registry, document, {"items": ["x", "y", "z", "w"]})
    assert result.status == RunStatus.SUCCEEDED
    assert result.outputs["results"] == ["x", "y", "z", "w"]
    assert time.perf_counter() - t0 < 0.8, "iterations did not run in parallel"


async def test_empty_list_and_item_validation(registry):
    document = for_each_doc([n("t", "text_transform")], [d("e3", "loop.item", "t.text")])
    result, _ = await run(registry, document, {"items": []})
    assert result.outputs == {"results": []} and result.node_status["t"] == NodeStatus.SKIPPED
    result, _ = await run(registry, document, {"items": ["ok", 5]})
    assert result.status == RunStatus.FAILED and result.error.code == "LOOP_ITEM_TYPE_MISMATCH"
    many = for_each_doc([n("t", "text_transform")], [d("e3", "loop.item", "t.text")], loop_cfg={"max_items": 2})
    result, _ = await run(registry, many, {"items": ["a", "b", "c"]})
    assert result.error.code == "LOOP_TOO_MANY_ITEMS"
    trunc = for_each_doc(
        [n("t", "text_transform")],
        [d("e3", "loop.item", "t.text")],
        loop_cfg={"max_items": 2, "on_too_many": "truncate"},
    )
    result, _ = await run(registry, trunc, {"items": ["a", "b", "c"]})
    assert result.outputs["results"] == ["A", "B"]


async def test_body_failure_fails_or_is_skipped(registry):
    body = [n("t", "text_transform"), n("bad", "tests.rt.boom")]
    edges = [d("e3", "loop.item", "t.text"), d("e4", "t.text", "bad.value")]
    result, obs = await run(registry, for_each_doc(body, edges, value_src="bad.value"), {"items": ["a", "b"]})
    assert result.status == RunStatus.FAILED
    assert result.error.code == "BOOM" and result.error.node_id == "bad" and "iteration 0" in result.error.message
    assert any(e.node_id == "end" and e.event_type.value == "node_failed" for e in obs.events)

    skipping = for_each_doc(body, edges, value_src="bad.value", loop_cfg={"on_item_error": "skip"})
    result, obs = await run(registry, skipping, {"items": ["a", "b"]})
    assert result.status == RunStatus.SUCCEEDED and result.outputs["results"] == [None, None]
    report = next(e for e in obs.events if e.node_id == "end" and e.event_type.value == "node_succeeded").data["loop"]
    assert [x["index"] for x in report["errors"]] == [0, 1]


def repeat_doc(operand, max_iterations=10):
    return doc(
        [
            n("start", "manual_input", key="start", value_type="Number"),
            n("loop", LOOP, mode="repeat", state_type="Number", max_iterations=max_iterations),
            n("add", "tests.rt.add", amount=3),
            n("check", "conditional", operator="less_than", operand=operand),
            n("end", END),
            n("out", "output", key="results"),
            n("last", "output", key="last"),
        ],
        [
            d("e1", "start.value", "loop.initial"),
            d("e2", "loop.scope", "end.scope"),
            d("e3", "loop.state", "add.value"),
            d("e4", "add.value", "end.value"),
            d("e5", "add.value", "check.value"),
            d("e6", "check.result", "end.continue"),
            d("e7", "end.results", "out.value"),
            d("e8", "end.last", "last.value"),
        ],
    )


async def test_repeat_until_condition_and_max_iterations(registry):
    result, _ = await run(registry, repeat_doc(10), {"start": 1})
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs == {"results": [4, 7, 10], "last": 10}
    result, obs = await run(registry, repeat_doc(1000, max_iterations=3), {"start": 1})
    assert result.outputs["results"] == [4, 7, 10]
    report = next(e for e in obs.events if e.node_id == "end" and e.event_type.value == "node_succeeded").data["loop"]
    assert report["stop_reason"] == "max_iterations"


async def test_nested_loops(registry):
    document = doc(
        [
            n("lists", "manual_input", key="lists", value_type="JSON"),
            n("outer", LOOP, item_type="JSON"),
            n("inner", LOOP, item_type="Number"),
            n("add", "tests.rt.add", amount=1),
            n("inner_end", END),
            n("outer_end", END),
            n("out", "output", key="results"),
        ],
        [
            d("e1", "lists.value", "outer.items"),
            d("e2", "outer.scope", "outer_end.scope"),
            d("e3", "outer.item", "inner.items"),
            d("e4", "inner.scope", "inner_end.scope"),
            d("e5", "inner.item", "add.value"),
            d("e6", "add.value", "inner_end.value"),
            d("e7", "inner_end.results", "outer_end.value"),
            d("e8", "outer_end.results", "out.value"),
        ],
    )
    assert validate_document(document, registry).valid
    plan = compile_plan(document, registry)
    assert plan.regions["outer_end"].order == ("inner", "inner_end")
    assert plan.regions["inner_end"].order == ("add",)
    result, obs = await run(registry, document, {"lists": [[1, 2], [3]]})
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs == {"results": [[2, 3], [4]]}
    iters = sorted(
        e.data["iteration"] for e in obs.events if e.node_id == "add" and e.event_type.value == "node_succeeded"
    )
    assert iters == [[0, 0], [0, 1], [1, 0]]


async def test_router_inside_loop_with_join(registry):
    tickets = ["My invoice is wrong", "App crashes on login", "Where is your office?"]
    document = doc(
        [
            n("tickets", "manual_input", key="tickets", value_type="JSON"),
            n("loop", LOOP, item_type="String"),
            n("route", "router"),
            n("billing", "text_template", template="[billing] {{ text }}"),
            n("tech", "text_template", template="[technical] {{ text }}"),
            n("other", "text_template", template="[general] {{ text }}"),
            n("join", "join", mode="concat"),
            n("end", END),
            n("out", "output", key="replies"),
        ],
        [
            d("e1", "tickets.value", "loop.items"),
            d("e2", "loop.scope", "end.scope"),
            d("e3", "loop.item", "route.value"),
            c("c1", "route", "billing", "billing"),
            c("c2", "route", "tech", "technical"),
            c("c3", "route", "other", "default"),
            d("e4", "loop.item", "billing.text"),
            d("e5", "loop.item", "tech.text"),
            d("e6", "loop.item", "other.text"),
            d("e7", "billing.text", "join.values"),
            d("e8", "tech.text", "join.values"),
            d("e9", "other.text", "join.values"),
            d("e10", "join.value", "end.value"),
            d("e11", "end.results", "out.value"),
        ],
    )
    report = validate_document(document, registry)
    assert report.valid, report.diagnostics
    result, _ = await run(registry, document, {"tickets": tickets})
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs["replies"] == [
        "[billing] My invoice is wrong",
        "[technical] App crashes on login",
        "[general] Where is your office?",
    ]


async def test_join_waits_for_all_control_paths(registry):
    document = doc(
        [
            n("x", "manual_input", key="x", value_type="Number"),
            n("big", "conditional", operator="greater_than", operand=5),
            n("small", "conditional", operator="less_than", operand=100),
            n("join", "join", min_values=0),
            n("out", "output", key="joined"),
        ],
        [
            d("e1", "x.value", "big.value"),
            d("e2", "x.value", "small.value"),
            c("c1", "big", "join", "true"),
            c("c2", "small", "join", "true"),
            d("e3", "join.count", "out.value"),
        ],
    )
    result, _ = await run(registry, document, {"x": 50})  # both true -> join runs
    assert result.outputs == {"joined": 0}
    result, obs = await run(registry, document, {"x": 1})  # only 'small' is true -> join skipped
    assert result.node_status["join"] == NodeStatus.SKIPPED
    assert any(e.node_id == "join" and e.data.get("reason") == "control_not_activated" for e in obs.events)


def test_loop_validation_rules(registry):
    unpaired = doc(
        [n("items", "manual_input", value_type="JSON"), n("loop", LOOP)], [d("e1", "items.value", "loop.items")]
    )
    assert Code.LOOP_UNPAIRED in codes(validate_document(unpaired, registry))

    leak = for_each_doc(
        [n("t", "text_transform"), n("peek", "output", key="peek")],
        [d("e3", "loop.item", "t.text"), d("e4", "t.text", "peek.value")],
    )
    diag = next(x for x in validate_document(leak, registry).diagnostics if x.code == Code.LOOP_REGION_LEAK)
    assert diag.node_id == "peek" and diag.edge_id == "e4"

    overlap = doc(
        [
            n("items", "manual_input", value_type="JSON"),
            n("a", LOOP),
            n("b", LOOP),
            n("mix", "collect"),
            n("a_end", END),
            n("b_end", END),
        ],
        [
            d("e1", "items.value", "a.items"),
            d("e2", "items.value", "b.items"),
            d("e3", "a.scope", "a_end.scope"),
            d("e4", "b.scope", "b_end.scope"),
            d("e5", "a.item", "mix.items"),
            d("e6", "b.item", "mix.items"),
            d("e7", "mix.items", "a_end.value"),
            d("e8", "mix.items", "b_end.value"),
        ],
    )
    assert Code.LOOP_OVERLAP in codes(validate_document(overlap, registry))


def test_router_branches_come_from_config(registry):
    def routed(branch):
        return doc(
            [
                n("v", "manual_input"),
                n("r", "router", routes=[{"name": "sales"}, {"name": "support"}]),
                n("o", "output"),
            ],
            [d("e1", "v.value", "r.value"), d("e2", "v.value", "o.value"), c("c1", "r", "o", branch)],
        )

    assert validate_document(routed("support"), registry).valid
    assert validate_document(routed("default"), registry).valid
    bad = validate_document(routed("billing"), registry)
    assert Code.EDGE_UNKNOWN_PORT in codes(bad)


async def test_router_llm_mode(registry, monkeypatch):
    from agenticmind_node_sdk import ModelUsage
    from agenticmind_node_sdk.testing import invoke_node
    from agenticmind_providers import ChatResponse, ToolCall

    flow = importlib.import_module("agenticmind_nodes_core.flow")

    class Scripted:
        def __init__(self, route):
            self.route = route
            self.request = None

        async def chat(self, request, *, api_key=None, base_url=None):
            self.request = request
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(id="1", name="choose_route", arguments={"route": self.route, "reason": "r"})],
                usage=ModelUsage(provider="x", model="m"),
            )

    cfg = {"mode": "llm", "provider": "mock", "routes": [{"name": "refunds", "description": "money back"}]}
    prov = Scripted("refunds")
    monkeypatch.setattr(flow, "get_provider", lambda name: prov)
    r = await invoke_node(flow.router, inputs={"value": "I want my money back"}, config=cfg)
    assert r.outputs["route"] == "refunds" and r.branches == frozenset({"refunds"})
    assert prov.request.tool_choice == "required"
    assert prov.request.tools[0].parameters["properties"]["route"]["enum"] == ["refunds", "default"]
    monkeypatch.setattr(flow, "get_provider", lambda name: Scripted("teleport"))
    r = await invoke_node(flow.router, inputs={"value": "x"}, config=cfg)
    assert r.branches == frozenset({"default"}) and "teleport" in r.outputs["reason"]

import asyncio
import time

import pytest
from agenticmind_runtime import (
    CancelToken,
    Engine,
    InMemoryObserver,
    MappingSecretResolver,
    NodeStatus,
    PlanError,
    RunStatus,
    compile_plan,
)
from conftest import c, d, doc, n


async def run(registry, document, inputs=None, **engine_kwargs):
    obs = InMemoryObserver()
    secrets = engine_kwargs.pop("secrets", None)
    plan = compile_plan(document, registry, secrets=secrets)
    result = await Engine(observer=obs, secrets=secrets, retry_backoff_seconds=0.01, **engine_kwargs).run(
        plan, inputs=inputs or {}
    )
    return result, obs


async def test_input_transform_output(registry):
    document = doc(
        [
            n("name", "manual_input", key="name"),
            n("greet", "text_template", template="Hello, {{ text }}!"),
            n("shout", "text_transform", operation="uppercase"),
            n("out", "output", key="greeting"),
        ],
        [d("e1", "name.value", "greet.text"), d("e2", "greet.text", "shout.text"), d("e3", "shout.text", "out.value")],
    )
    result, obs = await run(registry, document, {"name": "Ada"})
    assert result.status == RunStatus.SUCCEEDED
    assert result.outputs == {"greeting": "HELLO, ADA!"}
    assert obs.outputs["greet"] == {"text": "Hello, Ada!"}
    types = obs.types()
    assert types[0] == "run_started" and types[-1] == "run_succeeded"
    assert [e.seq for e in obs.events] == list(range(1, len(obs.events) + 1))
    started = [e.node_id for e in obs.events if e.event_type.value == "node_started"]
    assert started == ["name", "greet", "shout", "out"]


async def test_invalid_graph_cannot_be_planned(registry):
    with pytest.raises(PlanError) as exc:
        compile_plan(doc([n("o", "output")], []), registry)
    assert exc.value.report.errors[0].code.value == "PORT_MISSING_REQUIRED_INPUT"


def branching_doc():
    return doc(
        [
            n("x", "manual_input", key="x", value_type="Number"),
            n("cond", "conditional", operator="greater_than", operand=10),
            n("big", "text_template", template="big"),
            n("small", "text_template", template="small"),
            n("out_big", "output", key="big"),
            n("out_small", "output", key="small"),
        ],
        [
            d("e1", "x.value", "cond.value"),
            c("c1", "cond", "big", "true"),
            c("c2", "cond", "small", "false"),
            d("e2", "big.text", "out_big.value"),
            d("e3", "small.text", "out_small.value"),
        ],
    )


async def test_conditional_routing_and_skip_propagation(registry):
    result, obs = await run(registry, branching_doc(), {"x": 42})
    assert result.status == RunStatus.SUCCEEDED
    assert result.outputs == {"big": "big"}
    assert result.node_status["small"] == NodeStatus.SKIPPED
    assert result.node_status["out_small"] == NodeStatus.SKIPPED
    skipped = {e.node_id: e.data["reason"] for e in obs.events if e.event_type.value == "node_skipped"}
    assert skipped == {"small": "control_not_activated", "out_small": "upstream_skipped:value"}

    result, _ = await run(registry, branching_doc(), {"x": 1})
    assert result.outputs == {"small": "small"}


async def test_parallel_branches_and_explicit_join(registry):
    document = doc(
        [
            n("a", "delay", seconds=0.3),
            n("b", "delay", seconds=0.3),
            n("ta", "text_template", template="A"),
            n("tb", "text_template", template="B"),
            n("join", "collect"),
            n("out", "output", key="items"),
        ],
        [
            c("c1", "a", "ta"),
            c("c2", "b", "tb"),
            d("e2", "tb.text", "join.items"),
            d("e1", "ta.text", "join.items"),
            d("e3", "join.items", "out.value"),
        ],
    )
    t0 = time.perf_counter()
    result, _ = await run(registry, document)
    elapsed = time.perf_counter() - t0
    assert result.status == RunStatus.SUCCEEDED
    assert result.outputs == {"items": ["A", "B"]}  # edge-ID order, not completion order
    assert elapsed < 0.55, f"branches did not run concurrently ({elapsed:.2f}s)"


async def test_retry_on_transient_error(registry):
    result, obs = await run(registry, doc([n("f", "tests.rt.flaky", fail_times=2)], []))
    assert result.status == RunStatus.SUCCEEDED
    assert obs.types("f") == [
        "node_started", "node_retrying", "node_started", "node_retrying", "node_started", "node_succeeded"
    ]
    assert obs.outputs["f"] == {"value": "ok after 3"}


async def test_retries_exhausted(registry):
    result, obs = await run(registry, doc([n("f", "tests.rt.flaky", fail_times=5)], []))
    assert result.status == RunStatus.FAILED
    assert result.error.code == "RATE_LIMITED" and result.error.node_id == "f"
    assert obs.types("f").count("node_started") == 3


async def test_timeout_override(registry):
    node = {**n("slow", "delay", seconds=5), "execution": {"timeout_seconds": 0.1}}
    result, _ = await run(registry, doc([node], []))
    assert result.status == RunStatus.FAILED
    assert result.error.code == "NODE_TIMEOUT"


async def test_fail_fast_cancels_running_nodes(registry):
    document = doc([n("slow", "delay", seconds=5), n("bad", "tests.rt.boom")], [])
    t0 = time.perf_counter()
    result, obs = await run(registry, document)
    assert time.perf_counter() - t0 < 2
    assert result.status == RunStatus.FAILED and result.error.code == "BOOM"
    assert result.node_status["slow"] == NodeStatus.CANCELLED
    assert "node_cancelled" in obs.types("slow")


async def test_output_contract_enforced(registry):
    result, _ = await run(registry, doc([n("b", "tests.rt.bad_output")], []))
    assert result.error.code == "NODE_OUTPUT_TYPE_MISMATCH"


async def test_node_failure_from_missing_input(registry):
    document = doc([n("i", "manual_input", key="q"), n("o", "output")], [d("e", "i.value", "o.value")])
    result, _ = await run(registry, document, {})
    assert result.status == RunStatus.FAILED and result.error.code == "INPUT_MISSING"


async def test_cancellation(registry):
    obs = InMemoryObserver()
    plan = compile_plan(doc([n("slow", "delay", seconds=5)], []), registry)
    token = CancelToken()
    task = asyncio.create_task(Engine(observer=obs).run(plan, cancel=token))
    await asyncio.sleep(0.1)
    token.cancel()
    result = await asyncio.wait_for(task, 2)
    assert result.status == RunStatus.CANCELLED
    assert obs.types()[-1] == "run_cancelled"


async def test_secrets_never_reach_traces(registry):
    secret = "sk-very-secret-value"
    result, obs = await run(
        registry,
        doc([n("l", "tests.rt.leaky", api_key={"$secret": "MY_KEY"})], []),
        secrets=MappingSecretResolver({"MY_KEY": secret}),
    )
    assert result.status == RunStatus.SUCCEEDED
    dumped = "".join(e.model_dump_json() for e in obs.events) + str(obs.outputs)
    assert secret not in dumped
    assert "***" in dumped
    assert any(e.event_type.value == "node_event" and e.data["type"] == "debug" for e in obs.events)


async def test_llm_usage_in_trace(registry):
    document = doc(
        [n("q", "manual_input", key="q"), n("llm", "agenticmind.models.llm", model="demo"), n("o", "output")],
        [d("e1", "q.value", "llm.prompt"), d("e2", "llm.text", "o.value")],
    )
    result, obs = await run(registry, document, {"q": "hello there"})
    assert result.outputs == {"output": "[mock:demo] hello there"}
    done = next(e for e in obs.events if e.node_id == "llm" and e.event_type.value == "node_succeeded")
    assert done.usage.provider == "mock" and done.usage.input_tokens == 2

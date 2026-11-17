"""Suspension at Human Approval, checkpoints and resume (ADR-0013)."""

from agenticmind_node_sdk import NodeResult, OutputPort, PortType, SuspendNode, define_node
from agenticmind_registry import TrustLevel
from agenticmind_runtime import (
    Code,
    Engine,
    InMemoryObserver,
    MappingSecretResolver,
    NodeStatus,
    ResumeState,
    RunStatus,
    compile_plan,
    validate_document,
)
from conftest import c, d, doc, n

LOOP = "agenticmind.core.loop"
END = "agenticmind.core.loop_end"


def approval_doc(**approval_cfg):
    return doc(
        [
            n("draft", "manual_input", key="draft"),
            n("side", "text_transform", operation="uppercase"),
            n("side_out", "output", key="side"),
            n("review", "human_approval", **approval_cfg),
            n("published", "output", key="published"),
            n("reason", "output", key="reason"),
        ],
        [
            d("e1", "draft.value", "review.value"),
            d("e2", "draft.value", "side.text"),
            d("e3", "side.text", "side_out.value"),
            d("e4", "review.value", "published.value"),
            d("e5", "review.comment", "reason.value"),
            c("c1", "review", "published", "approved"),
            c("c2", "review", "reason", "rejected"),
        ],
    )


async def start(registry, document, inputs):
    obs = InMemoryObserver()
    plan = compile_plan(document, registry)
    result = await Engine(observer=obs).run(plan, run_id="run_1", inputs=inputs)
    return plan, obs, result


async def resume(plan, obs, inputs, responses):
    state = ResumeState(checkpoints=list(obs.checkpoints.values()), responses=responses, last_seq=obs.events[-1].seq)
    obs2 = InMemoryObserver()
    result = await Engine(observer=obs2).run(plan, run_id="run_1", inputs=inputs, resume=state)
    return obs2, result


async def test_run_waits_then_resumes_with_an_edit(registry):
    document = approval_doc(allow_edit=True)
    plan, obs, result = await start(registry, document, {"draft": "hello"})
    assert result.status == RunStatus.WAITING
    assert [w.node_id for w in result.waiting] == ["review"]
    wait = result.waiting[0]
    assert wait.request["value"] == "hello" and wait.request["allow_edit"] is True
    assert "value" in wait.response_schema["properties"]
    # independent work finished before the run paused; downstream of the approval is still pending
    assert result.node_status["side_out"] == NodeStatus.SUCCEEDED
    assert result.node_status["review"] == NodeStatus.WAITING
    assert result.node_status["published"] == NodeStatus.PENDING
    assert obs.types()[-1] == "run_waiting"
    assert set(obs.checkpoints) == {"draft", "side", "side_out"}

    obs2, result2 = await resume(plan, obs, {"draft": "hello"}, {"review": {"decision": "approved", "value": "HELLO!"}})
    assert result2.status == RunStatus.SUCCEEDED, result2.error
    assert result2.outputs == {"side": "HELLO", "published": "HELLO!"}
    assert result2.node_status["reason"] == NodeStatus.SKIPPED
    # restored nodes are not executed again, and seq continues
    started = {e.node_id for e in obs2.events if e.event_type.value == "node_started"}
    assert started == {"review", "published"}
    assert obs2.events[0].event_type.value == "run_resumed" and obs2.events[0].seq == obs.events[-1].seq + 1


async def test_rejection_takes_the_other_branch(registry):
    plan, obs, result = await start(registry, approval_doc(), {"draft": "hello"})
    _, result2 = await resume(
        plan, obs, {"draft": "hello"}, {"review": {"decision": "rejected", "comment": "Too short"}}
    )
    assert result2.status == RunStatus.SUCCEEDED
    assert result2.outputs == {"side": "HELLO", "reason": "Too short"}
    assert result2.node_status["published"] == NodeStatus.SKIPPED


async def test_resume_without_response_waits_again(registry):
    plan, obs, _ = await start(registry, approval_doc(), {"draft": "x"})
    obs2, result2 = await resume(plan, obs, {"draft": "x"}, {})
    assert result2.status == RunStatus.WAITING and [w.node_id for w in result2.waiting] == ["review"]


async def test_bad_response_fails_the_node(registry):
    plan, obs, _ = await start(registry, approval_doc(), {"draft": "x"})
    _, result2 = await resume(plan, obs, {"draft": "x"}, {"review": {"decision": "rejected"}})
    assert result2.status == RunStatus.FAILED and result2.error.code == "APPROVAL_INVALID_RESPONSE"


async def test_interrupted_run_resumes_from_checkpoints(registry):
    """A run cut short (server restart) continues from whatever was checkpointed."""
    document = doc(
        [n("a", "manual_input", key="a"), n("up", "text_transform", operation="uppercase"), n("o", "output", key="o")],
        [d("e1", "a.value", "up.text"), d("e2", "up.text", "o.value")],
    )
    obs = InMemoryObserver()
    plan = compile_plan(document, registry)
    await Engine(observer=obs).run(plan, run_id="r", inputs={"a": "hi"})
    partial = ResumeState(checkpoints=[obs.checkpoints["a"]], last_seq=4)
    obs2 = InMemoryObserver()
    result = await Engine(observer=obs2).run(plan, run_id="r", inputs={"a": "hi"}, resume=partial)
    assert result.status == RunStatus.SUCCEEDED and result.outputs == {"o": "HI"}
    assert {e.node_id for e in obs2.events if e.event_type.value == "node_started"} == {"up", "o"}


async def test_checkpoints_never_store_secret_values(registry):
    document = doc(
        [n("k", "tests.rt.leaky", api_key={"$secret": "API_KEY"}), n("o", "output")], [d("e1", "k.value", "o.value")]
    )
    obs = InMemoryObserver()
    plan = compile_plan(document, registry, secrets=MappingSecretResolver({"API_KEY": "sk-test-123"}))
    await Engine(observer=obs, secrets=MappingSecretResolver({"API_KEY": "sk-test-123"})).run(plan)
    assert obs.checkpoints["k"].restorable is False and obs.checkpoints["k"].outputs == {}
    assert "sk-test-123" not in obs.checkpoints["o"].model_dump_json() or not obs.checkpoints["o"].restorable


async def test_undeclared_suspension_is_an_error(registry):
    @define_node(
        type="tests.rt.sneaky_pause",
        version="1.0.0",
        display_name="Sneaky",
        category="test",
        package="tests",
        outputs=[OutputPort(name="value", type=PortType.STRING)],
    )
    async def sneaky(ctx, inputs, config) -> NodeResult:
        raise SuspendNode({"title": "wait"})

    registry.register(sneaky, trust=TrustLevel.CORE, source="tests")
    obs = InMemoryObserver()
    result = await Engine(observer=obs).run(compile_plan(doc([n("s", "tests.rt.sneaky_pause")], []), registry))
    assert result.status == RunStatus.FAILED and result.error.code == "NODE_SUSPEND_UNDECLARED"


def test_approval_inside_a_loop_is_rejected_by_validation(registry):
    document = doc(
        [
            n("items", "manual_input", key="items", value_type="JSON"),
            n("loop", LOOP),
            n("review", "human_approval"),
            n("end", END),
        ],
        [
            d("e1", "items.value", "loop.items"),
            d("e2", "loop.scope", "end.scope"),
            d("e3", "loop.item", "review.value"),
            d("e4", "review.value", "end.value"),
        ],
    )
    diag = next(x for x in validate_document(document, registry).diagnostics if x.code == Code.SUSPEND_IN_LOOP)
    assert diag.node_id == "review"

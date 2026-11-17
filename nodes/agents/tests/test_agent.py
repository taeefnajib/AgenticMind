import asyncio
import importlib

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import ModelUsage, NodeError, ToolHandle
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_agents import register as register_agents
from agenticmind_nodes_agents.agent import agent
from agenticmind_nodes_agents.tools import TOOL_IMPLEMENTATIONS
from agenticmind_nodes_core import register as register_core
from agenticmind_nodes_data import register as register_data
from agenticmind_nodes_rag import register as register_rag
from agenticmind_providers import ChatResponse, ToolCall
from agenticmind_registry import NodeRegistry, TrustLevel
from agenticmind_runtime import Engine, InMemoryObserver, LocalArtifactStore, RunStatus, compile_plan

CALC = {
    "kind": "tool",
    "tool_type": "calculator",
    "name": "calculator",
    "description": "math",
    "parameters": {
        "type": "object",
        "properties": {"expression": {"type": "string"}},
        "required": ["expression"],
        "additionalProperties": False,
    },
}
MOCK = {"provider": "mock", "model": "mock-1"}
# the package exports the Agent *node* as `agent`, so load the module explicitly
agent_mod = importlib.import_module("agenticmind_nodes_agents.agent")


class Scripted:
    """Fake provider replaying a list of responses and recording the requests it received."""

    name = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        nxt = self.responses.pop(0) if self.responses else self.responses_default()
        return nxt

    def responses_default(self):
        return reply("done")


def reply(text="", calls=(), tokens=(10, 5), cost=0.001):
    return ChatResponse(
        text=text,
        tool_calls=[ToolCall(id=f"c{i}", name=n, arguments=a) for i, (n, a) in enumerate(calls)],
        usage=ModelUsage(
            provider="scripted", model="m", input_tokens=tokens[0], output_tokens=tokens[1], estimated_cost_usd=cost
        ),
        finish_reason="tool_calls" if calls else "stop",
    )


@pytest.fixture
def scripted(monkeypatch):
    def install(*responses):
        prov = Scripted(*responses)
        monkeypatch.setattr(agent_mod, "get_provider", lambda name: prov)
        return prov

    return install


async def test_mock_agent_uses_calculator_end_to_end():
    r = await invoke_node(agent, inputs={"task": "calculator: 2+3*4", "tools": [CALC]}, config=MOCK)
    assert "14" in r.outputs["answer"]
    result = r.outputs["result"]
    assert result["steps"][0]["tool"] == "calculator" and result["steps"][0]["ok"] is True
    assert result["steps"][0]["arguments"] == {"expression": "2+3*4"}
    assert result["report"]["turns"] == 2 and result["report"]["tool_calls"] == 1
    assert result["report"]["stop_reason"] == "final_answer"
    roles = [m["role"] for m in r.outputs["transcript"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


async def test_bad_tool_calls_are_reported_back_to_the_model(scripted):
    prov = scripted(
        reply(calls=[("nope", {}), ("calculator", {"expression": 5}), ("calculator", {"expr": "1"})]),
        reply("Recovered: 2", tokens=(20, 3)),
    )
    r = await invoke_node(agent, inputs={"task": "t", "tools": [CALC]}, config=MOCK)
    steps = r.outputs["result"]["steps"]
    assert [s["ok"] for s in steps] == [False, False, False]
    assert "Unknown tool 'nope'" in steps[0]["error"]
    assert "is not of type 'string'" in steps[1]["error"]
    assert "required" in steps[2]["error"] or "Additional properties" in steps[2]["error"]
    tool_msgs = [m for m in prov.requests[1].messages if m.role == "tool"]
    assert len(tool_msgs) == 3 and all('"error"' in m.content for m in tool_msgs)
    assert r.outputs["answer"] == "Recovered: 2"
    assert r.usage.input_tokens == 30 and r.usage.estimated_cost_usd == pytest.approx(0.002)


async def test_tools_are_declared_to_the_model(scripted):
    prov = scripted(reply("hi"))
    await invoke_node(agent, inputs={"task": "t", "tools": [CALC], "context": "Company: Northwind"}, config=MOCK)
    req = prov.requests[0]
    assert [t.name for t in req.tools] == ["calculator"] and req.tool_choice == "auto"
    assert "Company: Northwind" in req.system


@pytest.mark.parametrize(
    "config,responses,reason",
    [
        ({"max_turns": 2}, [reply(calls=[("calculator", {"expression": "1"})])] * 3, "max_turns"),
        ({"max_tool_calls": 1}, [reply(calls=[("calculator", {"expression": "1"})] * 2)], "max_tool_calls"),
        (
            {"token_budget": 1000},
            [reply(calls=[("calculator", {"expression": "1"})], tokens=(900, 200))],
            "token_budget",
        ),
        ({"cost_budget_usd": 0.01}, [reply(calls=[("calculator", {"expression": "1"})], cost=0.02)], "cost_budget"),
    ],
)
async def test_limits_stop_the_agent(scripted, config, responses, reason):
    scripted(*responses)
    with pytest.raises(NodeError) as exc:
        await invoke_node(agent, inputs={"task": "t", "tools": [CALC]}, config={**MOCK, **config})
    assert exc.value.code == "AGENT_LIMIT_REACHED"
    scripted(*responses)
    r = await invoke_node(
        agent, inputs={"task": "t", "tools": [CALC]}, config={**MOCK, **config, "on_limit": "return_partial"}
    )
    assert r.outputs["result"]["report"]["stop_reason"] == reason


async def test_tool_timeout_and_duplicate_names(scripted, monkeypatch):
    async def sleepy(ctx, tool, args):
        await asyncio.sleep(5)

    monkeypatch.setitem(TOOL_IMPLEMENTATIONS, "sleepy", sleepy)
    slow = {**CALC, "tool_type": "sleepy", "name": "slow"}
    scripted(reply(calls=[("slow", {"expression": "1"})]), reply("ok"))
    r = await invoke_node(agent, inputs={"task": "t", "tools": [slow]}, config={**MOCK, "tool_timeout_seconds": 0.1})
    assert "timed out" in r.outputs["result"]["steps"][0]["error"]
    with pytest.raises(NodeError) as exc:
        await invoke_node(agent, inputs={"task": "t", "tools": [CALC, CALC]}, config=MOCK)
    assert exc.value.code == "DUPLICATE_TOOL_NAME"


async def test_unknown_tool_type_and_empty_answer(scripted):
    with pytest.raises(NodeError) as exc:
        await invoke_node(agent, inputs={"task": "t", "tools": [{**CALC, "tool_type": "teleport"}]}, config=MOCK)
    assert exc.value.code == "TOOL_UNKNOWN"
    scripted(reply(""))
    with pytest.raises(NodeError) as exc:
        await invoke_node(agent, inputs={"task": "t"}, config=MOCK)
    assert exc.value.code == "MODEL_EMPTY_RESPONSE"


def test_openrouter_requires_key():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        agent.parse_config({})
    assert agent.parse_config({"api_key": {"$secret": "OPENROUTER_API_KEY"}}).max_turns == 6
    assert ToolHandle.model_validate(CALC).name == "calculator"


# ---- full graph: agent searching indexed documents ----------------------------------------------------
HANDBOOK = (
    b"# Handbook\n\n## Annual leave\nEmployees receive 25 days of paid annual leave.\n\n"
    b"## Expenses\nHotels up to 180 EUR.\n"
)


def graph(file_id, emb):
    def n(i, t, **c):
        return {"id": i, "type": t, "version": "1.0.0", "config": c}

    def d(eid, s, t):
        (sn, sp), (tn, tp) = s.split("."), t.split(".")
        return {"id": eid, "kind": "data", "source": {"node": sn, "port": sp}, "target": {"node": tn, "port": tp}}

    return load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_agent", "name": "Agent"},
            "nodes": [
                n("emb", "agenticmind.rag.embedding_model", **emb),
                n("store", "agenticmind.rag.vector_store", collection="docs"),
                n("load", "agenticmind.data.document_loader", files=[file_id]),
                n("chunk", "agenticmind.rag.chunker", strategy="markdown", chunk_size=60, chunk_overlap=0),
                n("index", "agenticmind.rag.index_documents"),
                n("report", "agenticmind.core.output", key="report"),
                n("search", "agenticmind.agents.tool_knowledge_search", top_k=2),
                n("calc", "agenticmind.agents.tool_calculator"),
                n("task", "agenticmind.core.manual_input", key="task"),
                n("agent", "agenticmind.agents.agent", **MOCK),
                n("answer", "agenticmind.core.output", key="answer"),
            ],
            "edges": [
                d("e1", "load.documents", "chunk.documents"),
                d("e2", "chunk.chunks", "index.documents"),
                d("e3", "emb.model", "index.model"),
                d("e4", "store.store", "index.store"),
                d("e5", "index.report", "report.value"),
                d("e6", "emb.model", "search.model"),
                d("e7", "store.store", "search.store"),
                d("e8", "search.tool", "agent.tools"),
                d("e9", "calc.tool", "agent.tools"),
                d("e10", "task.value", "agent.task"),
                d("e11", "agent.result", "answer.value"),
            ],
        }
    )


@pytest.fixture
def registry():
    reg = NodeRegistry()
    for fn in (register_core, register_data, register_rag, register_agents):
        fn(lambda dfn: reg.register(dfn, trust=TrustLevel.CORE))
    return reg


async def test_agent_searches_indexed_documents_through_the_engine(registry, tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    info = artifacts.put_bytes("prj_agent", "handbook.md", HANDBOOK)
    doc = graph(info.id, {"provider": "hash", "dimensions": 256})
    engine = Engine(artifacts=artifacts, data_dir=tmp_path)
    assert (await engine.run(compile_plan(doc, registry, targets=["report"]))).status == RunStatus.SUCCEEDED

    obs = InMemoryObserver()
    plan = compile_plan(doc, registry, targets=["answer"])
    result = await Engine(observer=obs, artifacts=artifacts, data_dir=tmp_path).run(
        plan, inputs={"task": "search_documents: annual leave days"}
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    out = result.outputs["answer"]
    step = out["steps"][0]
    assert step["tool"] == "search_documents" and step["ok"] and step["arguments"] == {"query": "annual leave days"}
    assert "Annual leave" in out["answer"]  # the mock quotes the start of the tool result
    assert "25 days" in step["result_summary"]
    tool_events = [e for e in obs.events if e.event_type.value == "node_event" and e.data.get("type") == "tool_call"]
    assert len(tool_events) == 1 and tool_events[0].node_id == "agent"
    turns = [e for e in obs.events if e.data.get("type") == "agent_turn"]
    assert [t.data["turn"] for t in turns] == [1, 2]


def test_tool_handles_delegate_nested_secrets(registry, tmp_path):
    """The agent may resolve the embedding key carried through the knowledge-search tool handle."""
    doc = graph("art_x", {"provider": "openai_compatible", "model": "m", "api_key": {"$secret": "EMB_KEY"}})
    plan = compile_plan(doc, registry)
    assert "EMB_KEY" in plan.nodes["agent"].allowed_secrets
    assert plan.nodes["calc"].allowed_secrets == frozenset()

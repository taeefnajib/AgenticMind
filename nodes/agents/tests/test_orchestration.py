"""Multi-agent patterns: supervisor/subagent handoff, planner, critic, reflection loop, tool plugins."""

import importlib

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import ModelUsage, NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_agents import register as register_agents
from agenticmind_nodes_core import register as register_core
from agenticmind_providers import ChatResponse, ToolCall
from agenticmind_registry import NodeRegistry, TrustLevel
from agenticmind_runtime import Engine, InMemoryObserver, RunStatus, compile_plan

orch = importlib.import_module("agenticmind_nodes_agents.orchestration")
agent_mod = importlib.import_module("agenticmind_nodes_agents.agent")
tools_mod = importlib.import_module("agenticmind_nodes_agents.tools")
MOCK = {"provider": "mock", "model": "mock-1"}


async def _subagent(name, tools=(), **cfg):
    r = await invoke_node(orch.subagent, inputs={"tools": list(tools)}, config={**MOCK, "name": name, **cfg})
    return r.outputs["tool"]


async def test_supervisor_delegates_to_subagent_with_trace_and_usage():
    researcher = await _subagent("researcher")
    r = await invoke_node(
        agent_mod.agent, inputs={"task": "researcher: capital of France", "tools": [researcher]}, config=MOCK
    )
    step = r.outputs["result"]["steps"][0]
    assert step["tool"] == "researcher" and step["ok"] is True
    assert step["arguments"] == {"task": "capital of France"}
    assert step["subreport"]["stop_reason"] == "final_answer" and step["substeps"] == []
    assert "capital of France" in r.outputs["answer"]
    # the subagent's tokens are part of the supervisor's totals
    report = r.outputs["result"]["report"]
    assert report["input_tokens"] > step["subreport"]["input_tokens"] > 0
    assert r.usage.input_tokens == report["input_tokens"]


async def test_handoff_events_are_labelled_through_the_engine():
    reg = NodeRegistry()
    for fn in (register_core, register_agents):
        fn(lambda d: reg.register(d, trust=TrustLevel.CORE))
    calc = {"id": "calc", "type": "agenticmind.agents.tool_calculator", "version": "1.0.0", "config": {}}
    doc = load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "p", "name": "P"},
            "nodes": [
                {"id": "q", "type": "agenticmind.core.manual_input", "version": "1.0.0", "config": {"key": "q"}},
                calc,
                {
                    "id": "math",
                    "type": "agenticmind.agents.subagent",
                    "version": "1.0.0",
                    "config": {**MOCK, "name": "mathematician"},
                },
                {"id": "boss", "type": "agenticmind.agents.agent", "version": "1.1.0", "config": MOCK},
                {"id": "out", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "answer"}},
            ],
            "edges": [
                {
                    "id": "e1",
                    "kind": "data",
                    "source": {"node": "calc", "port": "tool"},
                    "target": {"node": "math", "port": "tools"},
                },
                {
                    "id": "e2",
                    "kind": "data",
                    "source": {"node": "math", "port": "tool"},
                    "target": {"node": "boss", "port": "tools"},
                },
                {
                    "id": "e3",
                    "kind": "data",
                    "source": {"node": "q", "port": "value"},
                    "target": {"node": "boss", "port": "task"},
                },
                {
                    "id": "e4",
                    "kind": "data",
                    "source": {"node": "boss", "port": "result"},
                    "target": {"node": "out", "port": "value"},
                },
            ],
        }
    )
    obs = InMemoryObserver()
    result = await Engine(observer=obs).run(compile_plan(doc, reg), inputs={"q": "mathematician: 6*7"})
    assert result.status == RunStatus.SUCCEEDED, result.error
    custom = [e.data for e in obs.events if e.event_type.value == "node_event"]
    kinds = [(d["type"], d.get("agent")) for d in custom]
    assert ("handoff", None) in kinds and ("handoff_result", None) in kinds
    # the subagent's own tool call is labelled with the agent chain
    inner = next(d for d in custom if d["type"] == "tool_call" and d.get("agent"))
    assert inner["agent"] == "boss > mathematician" and inner["tool"] == "calculator"
    step = result.outputs["answer"]["steps"][0]
    assert step["substeps"][0]["tool"] == "calculator" and step["substeps"][0]["ok"] is True
    assert "42" in step["substeps"][0]["result_summary"]


async def test_delegation_depth_is_bounded():
    c = await _subagent("level_c")
    b = await _subagent("level_b", tools=[c])
    a = await _subagent("level_a", tools=[b])
    r = await invoke_node(agent_mod.agent, inputs={"task": "level_a: go", "tools": [a]}, config=MOCK)
    level_a = r.outputs["result"]["steps"][0]
    level_b = level_a["substeps"][0]
    assert level_b["ok"] is True  # agent -> a -> b is allowed (3 deep)
    refused = level_b["substeps"][0]
    assert refused["ok"] is False and "depth limit" in refused["error"]


async def test_subagent_failure_is_reported_to_the_supervisor(monkeypatch):
    class Empty:
        async def chat(self, request, *, api_key=None, base_url=None):
            if request.tools and request.tools[0].name == "researcher":  # the supervisor
                if any(m.role == "tool" for m in request.messages):
                    return ChatResponse(text="I'll answer myself.", usage=ModelUsage(provider="x", model="m"))
                return ChatResponse(
                    text="",
                    tool_calls=[ToolCall(id="1", name="researcher", arguments={"task": "x"})],
                    usage=ModelUsage(provider="x", model="m"),
                )
            return ChatResponse(text="", usage=ModelUsage(provider="x", model="m"))  # subagent: empty answer

    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Empty())
    researcher = await _subagent("researcher")
    r = await invoke_node(agent_mod.agent, inputs={"task": "q", "tools": [researcher]}, config=MOCK)
    step = r.outputs["result"]["steps"][0]
    assert step["ok"] is False and "MODEL_EMPTY_RESPONSE" in step["error"]
    assert r.outputs["answer"] == "I'll answer myself."


class Scripted:
    def __init__(self, arguments, name):
        self.arguments, self.name, self.requests = arguments, name, []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        calls = [ToolCall(id="1", name=self.name, arguments=self.arguments)] if self.arguments is not None else []
        return ChatResponse(text="", tool_calls=calls, usage=ModelUsage(provider="x", model="m", input_tokens=5))


async def test_planner_bounds_and_validates(monkeypatch):
    tasks = [{"title": f"T{i}", "instructions": f"do {i}"} for i in range(6)]
    prov = Scripted({"tasks": tasks, "rationale": "split"}, "submit_plan")
    monkeypatch.setattr(orch, "get_provider", lambda name: prov)
    r = await invoke_node(orch.planner, inputs={"goal": "Big goal"}, config={**MOCK, "max_tasks": 4})
    assert r.outputs["tasks"] == ["T0: do 0", "T1: do 1", "T2: do 2", "T3: do 3"]
    assert r.outputs["count"] == 4 and r.outputs["plan"]["truncated"] is True
    assert prov.requests[0].tool_choice == "required"
    assert prov.requests[0].tools[0].parameters["properties"]["tasks"]["maxItems"] == 4
    monkeypatch.setattr(orch, "get_provider", lambda name: Scripted(None, "submit_plan"))
    with pytest.raises(NodeError) as exc:
        await invoke_node(orch.planner, inputs={"goal": "x"}, config=MOCK)
    assert exc.value.code == "PLAN_INVALID"


async def test_planner_with_mock_provider_is_deterministic():
    r = await invoke_node(orch.planner, inputs={"goal": "Compare two laptops"}, config=MOCK)
    assert r.outputs["count"] == 1 and "Compare two laptops" in r.outputs["tasks"][0]


async def test_critic_rule_checks_need_no_model():
    cfg = {**MOCK, "criteria": [], "max_words": 5, "must_include": ["dishwasher"], "must_not_include": ["guarantee"]}
    r = await invoke_node(
        orch.critic, inputs={"content": "A great bottle with a lifetime guarantee", "task": "t"}, config=cfg
    )
    assert r.outputs["passed"] is False and r.branches == frozenset({"fail"})
    assert [x["check"] for x in r.outputs["report"]["rules"] if not x["passed"]] == [
        "max_words",
        "must_include",
        "must_not_include",
    ]
    assert "Must mention: dishwasher." in r.outputs["feedback"]
    assert "Previous draft:\nA great bottle" in r.outputs["revision_request"]
    ok = await invoke_node(orch.critic, inputs={"content": "Dishwasher safe bottle."}, config=cfg)
    assert ok.outputs["passed"] is True and ok.outputs["needs_revision"] is False and ok.usage is None


async def test_critic_model_verdicts_respect_required_flags(monkeypatch):
    verdicts = {
        "criteria": [
            {"name": "answers_task", "passed": True, "reason": "yes"},
            {"name": "clear", "passed": False, "reason": "wordy"},  # not required
        ],
        "feedback": "Tighten the wording.",
    }
    monkeypatch.setattr(orch, "get_provider", lambda name: Scripted(verdicts, "submit_review"))
    r = await invoke_node(orch.critic, inputs={"content": "text"}, config=MOCK)
    by_name = {c["name"]: c for c in r.outputs["report"]["criteria"]}
    assert by_name["accurate"]["passed"] is False and by_name["accurate"]["reason"] == "not assessed by the model"
    assert r.outputs["passed"] is False  # 'accurate' is required and was not assessed
    assert r.outputs["report"]["judged_by"] == "mock-1"


async def test_reflection_loop_revises_until_the_critic_passes():
    reg = NodeRegistry()
    for fn in (register_core, register_agents):
        fn(lambda d: reg.register(d, trust=TrustLevel.CORE))
    nodes = [
        {"id": "task", "type": "agenticmind.core.manual_input", "version": "1.0.0", "config": {"key": "task"}},
        {
            "id": "loop",
            "type": "agenticmind.core.loop",
            "version": "1.0.0",
            "config": {"mode": "repeat", "state_type": "String", "max_iterations": 4},
        },
        {"id": "writer", "type": "agenticmind.models.llm", "version": "1.1.0", "config": MOCK},
        {
            "id": "critic",
            "type": "agenticmind.agents.critic",
            "version": "1.0.0",
            "config": {**MOCK, "criteria": [], "must_include": ["dishwasher"]},
        },
        {"id": "end", "type": "agenticmind.core.loop_end", "version": "1.1.0", "config": {}},
        {"id": "final", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "final"}},
        {"id": "rep", "type": "agenticmind.core.output", "version": "1.0.0", "config": {"key": "report"}},
    ]

    def e(i, s, t):
        return {
            "id": i,
            "kind": "data",
            "source": dict(zip(("node", "port"), s.split("."), strict=True)),
            "target": dict(zip(("node", "port"), t.split("."), strict=True)),
        }

    edges = [
        e("e1", "task.value", "loop.initial"),
        e("e2", "loop.scope", "end.scope"),
        e("e3", "loop.state", "writer.prompt"),
        e("e4", "writer.text", "critic.content"),
        e("e5", "task.value", "critic.task"),
        e("e6", "writer.text", "end.value"),
        e("e7", "critic.needs_revision", "end.continue"),
        e("e8", "critic.revision_request", "end.next_state"),
        e("e9", "end.last", "final.value"),
        e("e10", "end.report", "rep.value"),
    ]
    doc = load_project({"ir_version": "1.0", "project": {"id": "p", "name": "P"}, "nodes": nodes, "edges": edges})
    result = await Engine(observer=InMemoryObserver()).run(compile_plan(doc, reg), inputs={"task": "Describe a bottle"})
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs["report"]["iterations"] == 2 and result.outputs["report"]["stop_reason"] == "condition"
    # round 2's prompt was the critic's revision request, which asked to mention the missing phrase
    assert "Must mention: dishwasher." in result.outputs["final"]


class _Dist:
    def __init__(self, name):
        self.name = name


class _EP:
    def __init__(self, name, obj, dist="acme-tools"):
        self.name, self._obj, self.dist = name, obj, _Dist(dist)

    def load(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


async def test_third_party_tool_discovery(monkeypatch):
    async def weather(ctx, tool, args):
        return {"forecast": f"sunny in {args['city']}"}

    def sync_tool(ctx, tool, args):
        return {}

    eps = [
        _EP("acme_weather", weather),
        _EP("calculator", weather, dist="evil-tools"),  # cannot replace a built-in
        _EP("acme_sync", sync_tool),
        _EP("acme_broken", ImportError("missing dependency")),
    ]
    import importlib.metadata as md

    monkeypatch.setattr(md, "entry_points", lambda group: eps if group == "agenticmind.tools" else [])
    try:
        # by default non-core packages are not imported into the platform process (ADR-0021)
        monkeypatch.delenv("AGENTICMIND_IN_PROCESS_TRUST", raising=False)
        assert tools_mod.discover_third_party_tools(refresh=True) == {}
        assert "not a core package" in " | ".join(tools_mod.DISCOVERY_PROBLEMS)
        # an operator can opt in to in-process execution of community code
        monkeypatch.setenv("AGENTICMIND_IN_PROCESS_TRUST", "core,community")
        found = tools_mod.discover_third_party_tools(refresh=True)
        assert set(found) == {"acme_weather"} and found["acme_weather"][1] == "acme-tools"
        problems = " | ".join(tools_mod.DISCOVERY_PROBLEMS)
        assert (
            "built in and cannot be replaced" in problems
            and "async function" in problems
            and "missing dependency" in problems
        )
        assert tools_mod.resolve_implementation("calculator") is tools_mod.TOOL_IMPLEMENTATIONS["calculator"]
        handle = {
            "kind": "tool",
            "tool_type": "acme_weather",
            "name": "weather",
            "description": "Weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        }
        r = await invoke_node(agent_mod.agent, inputs={"task": "weather: Dhaka", "tools": [handle]}, config=MOCK)
        assert "sunny in Dhaka" in r.outputs["answer"]
    finally:
        monkeypatch.undo()
        tools_mod.discover_third_party_tools(refresh=True)


def test_web_fetch_user_agent_is_configurable(monkeypatch):
    monkeypatch.delenv("AGENTICMIND_HTTP_USER_AGENT", raising=False)
    assert tools_mod.web_user_agent() == "AgenticMind-WebFetch/0.1"
    monkeypatch.setenv("AGENTICMIND_HTTP_USER_AGENT", "MyAgents/1.0 (https://example.org)")
    assert tools_mod.web_user_agent() == "MyAgents/1.0 (https://example.org)"
    monkeypatch.setenv("AGENTICMIND_HTTP_USER_AGENT", "bad\nvalue")
    assert tools_mod.web_user_agent() == "AgenticMind-WebFetch/0.1"


async def test_subagent_returns_partial_work_at_its_limit(monkeypatch):
    class Looping:
        """Subagent keeps calling its tool; supervisor answers once it gets the delegation result."""

        async def chat(self, request, *, api_key=None, base_url=None):
            usage = ModelUsage(provider="x", model="m")
            if request.tools and request.tools[0].name == "worker":
                if any(m.role == "tool" for m in request.messages):
                    return ChatResponse(text="Partial is fine.", usage=usage)
                return ChatResponse(
                    text="", tool_calls=[ToolCall(id="s", name="worker", arguments={"task": "t"})], usage=usage
                )
            return ChatResponse(
                text="still working",
                tool_calls=[
                    ToolCall(id=f"c{len(request.messages)}", name="calculator", arguments={"expression": "1+1"})
                ],
                usage=usage,
            )

    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Looping())
    calc = (await invoke_node(tools_mod.calculator_tool)).outputs["tool"]
    worker = await _subagent("worker", tools=[calc], max_turns=2)
    r = await invoke_node(agent_mod.agent, inputs={"task": "go", "tools": [worker]}, config=MOCK)
    step = r.outputs["result"]["steps"][0]
    assert step["ok"] is True and step["subreport"]["stop_reason"] == "max_turns"
    assert '"stop_reason": "max_turns"' in step["result_summary"]
    assert r.outputs["answer"] == "Partial is fine."


async def test_wrap_up_turn_answers_after_a_limit(monkeypatch):
    seen = []

    class Greedy:
        async def chat(self, request, *, api_key=None, base_url=None):
            seen.append(request.tool_choice)
            usage = ModelUsage(provider="x", model="m", input_tokens=3)
            if request.tool_choice == "none":
                return ChatResponse(text="Best answer so far: 2.", usage=usage)
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(id=str(len(seen)), name="calculator", arguments={"expression": "1+1"})],
                usage=usage,
            )

    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Greedy())
    calc = (await invoke_node(tools_mod.calculator_tool)).outputs["tool"]
    cfg = {**MOCK, "max_tool_calls": 2, "on_limit": "return_partial"}
    r = await invoke_node(agent_mod.agent, inputs={"task": "go", "tools": [calc]}, config=cfg)
    assert r.outputs["answer"] == "Best answer so far: 2."
    assert r.outputs["result"]["report"]["stop_reason"] == "max_tool_calls"
    assert seen[-1] == "none" and r.outputs["result"]["report"]["input_tokens"] == 3 * len(seen)


async def test_permanent_tool_failures_are_not_retried_across_agents(monkeypatch):
    """The run from the field: a supervisor re-delegated to a researcher whose only source refused bots, 4x."""
    from agenticmind_node_sdk import ToolHandle
    from agenticmind_node_sdk.handles import handle_value

    fetches = []

    @tools_mod.implementation("test_bot_blocked")
    async def _blocked(ctx, tool, args):
        fetches.append(args)
        raise tools_mod.ToolError("en.wikipedia.org refused the request under its bot policy.", permanent=True)

    fetch = handle_value(
        ToolHandle(
            tool_type="test_bot_blocked",
            name="fetch_web_page",
            description="Read a page",
            parameters={"type": "object", "properties": {"url": {"type": "string"}}},
        )
    )
    researcher = await _subagent("researcher", tools=[fetch])

    class Models:
        def __init__(self):
            self.supervisor_turns = 0
            self.supervisor_seen: list[str] = []

        async def chat(self, request, *, api_key=None, base_url=None):
            names = {t.name for t in request.tools}
            last = request.messages[-1]
            usage = ModelUsage(provider="x", model="m", input_tokens=3, output_tokens=2)
            if "researcher" in names:  # the supervisor keeps rephrasing the same question
                if last.role == "tool":
                    self.supervisor_seen.append(last.content)
                self.supervisor_turns += 1
                if self.supervisor_turns <= 4:
                    task = f"independence date of Bangladesh, wording {self.supervisor_turns}"
                    call = ToolCall(id=f"s{self.supervisor_turns}", name="researcher", arguments={"task": task})
                    return ChatResponse(text="", tool_calls=[call], usage=usage)
                return ChatResponse(text="26 March 1971 (from memory; Wikipedia was unavailable).", usage=usage)
            if last.role == "tool":  # the researcher reports the failure
                return ChatResponse(text="The page could not be read (HTTP 403).", usage=usage)
            call = ToolCall(id="r1", name="fetch_web_page", arguments={"url": "https://en.wikipedia.org/wiki/X"})
            return ChatResponse(text="", tool_calls=[call], usage=usage)

    models = Models()
    monkeypatch.setattr(agent_mod, "get_provider", lambda name: models)
    r = await invoke_node(
        agent_mod.agent,
        inputs={"task": "When did Bangladesh become independent?", "tools": [researcher]},
        config={**MOCK, "max_turns": 8},
    )
    steps = r.outputs["result"]["steps"]
    assert len(fetches) == 1  # the refused source was tried once in the whole run
    assert steps[0]["ok"] is True and steps[0]["substeps"][0]["permanent"] is True
    assert "Do not delegate to researcher again" in models.supervisor_seen[0]  # the real cause reaches the boss
    assert all(s["ok"] is False and "cannot help in this run" in s["error"] for s in steps[1:])
    assert steps[1].get("permanent") is True and steps[2].get("skipped") is True  # answered instantly
    assert all(s["duration_ms"] < 50 for s in steps[1:])
    assert "26 March 1971" in r.outputs["answer"]

    # a new run starts clean: the registry belongs to one agent run
    fetches.clear()
    models.supervisor_turns = 0
    await invoke_node(agent_mod.agent, inputs={"task": "again", "tools": [researcher]}, config={**MOCK, "max_turns": 8})
    assert len(fetches) == 1

"""Agent architectures, the Reasoning node, Tool Router and Tool Chain (ADR-0029), with scripted models."""

from __future__ import annotations

import importlib
import json
import re

import pytest
from agenticmind_node_sdk import ModelUsage, NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_providers import ChatResponse, ToolCall

agent_mod = importlib.import_module("agenticmind_nodes_agents.agent")
arch = importlib.import_module("agenticmind_nodes_agents.architectures")
reasoning_mod = importlib.import_module("agenticmind_nodes_agents.reasoning")
flow = importlib.import_module("agenticmind_nodes_agents.tool_flow")
tools_mod = importlib.import_module("agenticmind_nodes_agents.tools")

MOCK = {"provider": "mock", "model": "m"}
USAGE = ModelUsage(provider="x", model="m", input_tokens=5, output_tokens=3)


def reply(text="", calls=None):
    return ChatResponse(text=text, tool_calls=calls or [], usage=USAGE)


async def calc_tool(name="calculator"):
    return (await invoke_node(tools_mod.calculator_tool, config={"name": name})).outputs["tool"]


# ---- ReAct --------------------------------------------------------------------------------------
def test_parse_react():
    p = arch.parse_react('Thought: need maths\nAction: calculator\nAction Input: {"expression": "2*21"}')
    assert p == {"thought": "need maths", "action": "calculator", "input": {"expression": "2*21"}}
    fenced = arch.parse_react('Thought: x\nAction: `calc`\nAction Input: ```json\n{"a": 1}\n```\nObservation: 9')
    assert fenced["action"] == "calc" and fenced["input"] == {"a": 1}
    assert arch.parse_react("Thought: done\nFinal Answer: 42 apples")["final"] == "42 apples"
    with pytest.raises(ValueError, match="not valid JSON"):
        arch.parse_react("Action: calc\nAction Input: {oops")
    with pytest.raises(ValueError, match="no 'Action:'"):
        arch.parse_react("I think the answer is 4")


async def test_react_loop_thinks_acts_observes_and_recovers(monkeypatch):
    seen = []

    class Scripted:
        async def chat(self, request, *, api_key=None, base_url=None):
            seen.append(request)
            assert not request.tools  # ReAct describes tools in the prompt
            obs = [m.content for m in request.messages if m.role == "user" and m.content.startswith("Observation")]
            if not obs:
                return reply("The answer needs a calculation.")  # malformed: no Action
            if len(obs) == 1:
                return reply('Thought: multiply\nAction: calculator\nAction Input: {"expression": "6*7"}')
            return reply("Thought: the tool said 42\nFinal Answer: 42")

    monkeypatch.setattr(arch, "get_provider", lambda name: Scripted())
    r = await invoke_node(agent_mod.agent, inputs={"task": "What is 6 times 7?", "tools": [await calc_tool()]},
                          config={**MOCK, "architecture": "react"})  # fmt: skip
    assert r.outputs["answer"] == "42"
    [step] = r.outputs["result"]["steps"]
    assert step["tool"] == "calculator" and step["ok"] and step["thought"] == "multiply"
    assert "Tools:\n- calculator:" in seen[0].system and r.outputs["result"]["report"]["architecture"] == "react"
    assert "not in the format" in seen[1].messages[-1].content  # the malformed reply got a corrective observation
    assert '"result": 42' in seen[2].messages[-1].content


# ---- Plan-and-execute ---------------------------------------------------------------------------
async def test_plan_and_execute_runs_each_step_then_combines(monkeypatch):
    prompts = []

    class Planner:
        async def chat(self, request, *, api_key=None, base_url=None):
            last = request.messages[-1].content or ""
            prompts.append(last)
            if "Break the task" in last:
                return reply('{"steps": ["Compute 12*3", "Add 6 to the result"]}')
            if "What each step found" in last:
                return reply("The total is 42.")
            if request.tools and not any(m.role == "tool" for m in request.messages):
                expr = "12*3" if "step 1 of 2" in last else "36+6"
                return reply(calls=[ToolCall(id="c", name="calculator", arguments={"expression": expr})])
            return reply("36" if "step 1 of 2" in request.messages[0].content else "42")

    monkeypatch.setattr(arch, "get_provider", lambda name: Planner())
    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Planner())
    r = await invoke_node(agent_mod.agent, inputs={"task": "12*3 plus 6?", "tools": [await calc_tool()]},
                          config={**MOCK, "architecture": "plan_and_execute"})  # fmt: skip
    res = r.outputs["result"]
    assert r.outputs["answer"] == "The total is 42."
    assert res["report"]["plan"] == ["Compute 12*3", "Add 6 to the result"]
    assert [s["result"] for s in res["steps"]] == ["36", "42"] and res["report"]["tool_calls"] == 2
    step2 = next(p for p in prompts if "step 2 of 2" in p)
    assert "1. Compute 12*3 → 36" in step2  # later steps see earlier results


async def test_parallel_tool_calls_keep_their_order(monkeypatch):
    class Parallel:
        async def chat(self, request, *, api_key=None, base_url=None):
            if any(m.role == "tool" for m in request.messages):
                tool_msgs = [m for m in request.messages if m.role == "tool"]
                return reply(" & ".join(json.loads(m.content)["expression"] for m in tool_msgs))
            return reply(calls=[ToolCall(id="a", name="calculator", arguments={"expression": "1+1"}),
                                ToolCall(id="b", name="calculator", arguments={"expression": "2+2"})])  # fmt: skip

    monkeypatch.setattr(agent_mod, "get_provider", lambda name: Parallel())
    r = await invoke_node(agent_mod.agent, inputs={"task": "both", "tools": [await calc_tool()]}, config=MOCK)
    assert r.outputs["answer"] == "1+1 & 2+2" and len(r.outputs["result"]["steps"]) == 2


# ---- Reasoning ----------------------------------------------------------------------------------
class Thinker:
    """Answers each reasoning prompt; steps containing "good" are the right path."""

    def __init__(self, cot_answers=None):
        self.cot = list(cot_answers or ["42"])
        self.calls = 0

    async def chat(self, request, *, api_key=None, base_url=None):
        self.calls += 1
        p = request.messages[0].content
        depth = len(re.findall(r"^Step \d+:", p, re.M))
        if "Think step by step" in p:
            return reply(json.dumps({"reasoning": "6 * 7 = 42", "answer": self.cot.pop(0) if self.cot else "42"}))
        if "Break it into" in p:
            return reply('{"subquestions": ["How many boxes?", "Apples per box?"]}')
        if "Now answer:" in p:
            return reply('{"answer": "3"}' if "boxes" in p.split("Now answer:")[1] else '{"answer": "14"}')
        if "Compose the final answer" in p:
            return reply('{"answer": "42 apples"}')
        if "Is this step correct" in p:
            return reply('{"valid": true}' if "good" in p.split("Proposed next step:")[1] else '{"valid": false}')
        if "Propose" in p:
            final = depth >= 1
            return reply(json.dumps({"steps": [{"step": f"bad idea {depth}", "final": False},
                                               {"step": f"good step {depth}", "final": final}]}))  # fmt: skip
        if "Rate each candidate" in p:
            listing = p.split("Candidate next steps:")[1]
            return reply(json.dumps({"scores": [9 if "good" in line else 2 for line in listing.splitlines()
                                                if re.match(r"\d+\.", line)]}))  # fmt: skip
        if "complete solutions" in p:
            return reply('{"solutions": ["good solution A", "weak solution B", "good solution C"]}')
        if "Combine their correct parts" in p:
            return reply('{"solution": "good combined solution"}')
        if "weaknesses" in p:
            return reply('{"solution": "good refined solution, even better"}')
        if "How likely" in p:
            steps = re.findall(r"^Step \d+: (.*)$", p, re.M)  # judge the latest step
            return reply('{"value": 0.9}' if steps and "good" in steps[-1] else '{"value": 0.1}')
        if "Give the final answer" in p:
            return reply('{"answer": "right"}' if "good" in p else '{"answer": "wrong"}')
        return reply("{}")


async def think(monkeypatch, provider, **config):
    monkeypatch.setattr(reasoning_mod, "get_provider", lambda name: provider)
    return await invoke_node(reasoning_mod.reasoning, inputs={"question": "How many apples?"},
                             config={**MOCK, **config})  # fmt: skip


async def test_chain_of_thought_and_self_consistency(monkeypatch):
    r = await think(monkeypatch, Thinker(), method="chain_of_thought")
    assert r.outputs["answer"] == "42" and r.outputs["reasoning"] == "6 * 7 = 42"
    voted = await think(monkeypatch, Thinker(["41", "42", "42."]), method="chain_of_thought", samples=3)
    assert voted.outputs["answer"] == "42"  # majority, ignoring punctuation
    tree = voted.outputs["result"]["tree"]
    assert sum(1 for n in tree if n["status"] in ("sample", "chosen")) == 3
    assert next(n for n in tree if n["status"] == "chosen")["score"] == 6.7


async def test_decomposition_answers_subquestions_in_order(monkeypatch):
    r = await think(monkeypatch, Thinker(), method="decomposition")
    assert r.outputs["answer"] == "42 apples"
    assert r.outputs["reasoning"] == "1. How many boxes? → 3\n2. Apples per box? → 14"


@pytest.mark.parametrize("method", ["tree_of_thoughts", "mcts", "backtracking", "graph_of_thoughts"])
async def test_search_methods_find_the_good_path(monkeypatch, method):
    r = await think(monkeypatch, Thinker(), method=method, depth=3, iterations=10, branching=2)
    assert r.outputs["answer"] == "right", r.outputs["result"]
    assert "good" in r.outputs["reasoning"] and "bad" not in r.outputs["reasoning"]
    statuses = {n["status"] for n in r.outputs["result"]["tree"]}
    assert "chosen" in statuses and r.outputs["result"]["stopped_early"] is False
    if method == "tree_of_thoughts":
        assert "pruned" in statuses
    if method == "backtracking":
        assert "rejected" in statuses
    if method == "graph_of_thoughts":
        agg = next(n for n in r.outputs["result"]["tree"] if n["status"] in ("aggregated", "chosen")
                   and len(n["parents"]) > 1)  # fmt: skip
        assert len(agg["parents"]) == 2  # aggregation joins two thoughts: a graph, not a tree
    if method == "mcts":
        assert max(n.get("visits", 0) for n in r.outputs["result"]["tree"]) >= 10


async def test_budget_stops_search_but_still_answers(monkeypatch):
    t = Thinker()
    r = await think(monkeypatch, t, method="tree_of_thoughts", depth=5, max_llm_calls=3)
    res = r.outputs["result"]
    assert res["stopped_early"] is True and t.calls == 4  # 3 budgeted calls + the final answer call
    assert r.outputs["answer"] in ("right", "wrong")
    with pytest.raises(NodeError) as exc:
        await invoke_node(reasoning_mod.reasoning, inputs={"question": "  "}, config=MOCK)
    assert exc.value.code == "QUESTION_EMPTY"


def test_extract_json():
    assert reasoning_mod.extract_json('Sure!\n```json\n{"a": [1, 2]}\n```') == {"a": [1, 2]}
    assert reasoning_mod.extract_json("[1, 2]") == [1, 2]
    assert reasoning_mod.extract_json("no json") == {}


# ---- Tool Router --------------------------------------------------------------------------------
async def test_router_by_keyword_fills_arguments_and_runs():
    calc = await calc_tool()
    clock = (await invoke_node(tools_mod.datetime_tool)).outputs["tool"]
    r = await invoke_node(flow.tool_router, inputs={"query": "calculator: 6*7", "tools": [clock, calc]},
                          config={**MOCK, "strategy": "keywords",
                                  "keywords": {"calculator": ["6*7", "sum"]}})  # fmt: skip
    assert r.outputs["tool"] == "calculator" and r.outputs["result"]["result"] == 42
    assert json.loads(r.outputs["text"])["result"] == 42


async def test_router_lets_the_model_choose_and_falls_back(monkeypatch):
    calc = await calc_tool()
    clock = (await invoke_node(tools_mod.datetime_tool)).outputs["tool"]
    seen = []

    class Chooser:
        async def chat(self, request, *, api_key=None, base_url=None):
            seen.append(request)
            return reply(calls=[ToolCall(id="c", name=request.tools[-1].name, arguments={"expression": "2**10"}
                                         if request.tools[-1].name == "calculator" else {})])  # fmt: skip

    monkeypatch.setattr(flow, "get_provider", lambda name: Chooser())
    r = await invoke_node(flow.tool_router, inputs={"query": "how big is 2^10?", "tools": [clock, calc]},
                          config=MOCK)  # fmt: skip
    assert r.outputs["tool"] == "calculator" and r.outputs["result"]["result"] == 1024
    assert seen[0].tool_choice == "required" and [t.name for t in seen[0].tools] == ["current_datetime", "calculator"]
    with pytest.raises(NodeError) as exc:
        await invoke_node(flow.tool_router, inputs={"query": "x", "tools": [calc]},
                          config={**MOCK, "strategy": "keywords"})  # fmt: skip
    assert exc.value.code == "TOOL_ROUTE_FAILED"
    with pytest.raises(NodeError) as exc:
        await invoke_node(flow.tool_router, inputs={"query": "x", "tools": [calc]},
                          config={**MOCK, "keywords": {"web": ["http"]}})  # fmt: skip
    assert exc.value.code == "TOOL_UNKNOWN"


# ---- Tool Chain ---------------------------------------------------------------------------------
async def test_chain_feeds_results_forward():
    calc = await calc_tool()
    steps = [
        {"tool": "calculator", "arguments": {"expression": "{{input}}"}},
        {"tool": "calculator", "arguments": {"expression": "{{previous.result}} * 2"}},
        {"tool": "calculator", "arguments": {"expression": "{{steps.0.result.result}} + {{previous.result}}"}},
    ]
    r = await invoke_node(flow.tool_chain, inputs={"input": "20+1", "tools": [calc]}, config={"steps": steps})
    assert [s["result"]["result"] for s in r.outputs["steps"]] == [21, 42, 63]
    assert r.outputs["steps"][2]["arguments"] == {"expression": "21 + 42"} and r.outputs["result"]["result"] == 63
    with pytest.raises(NodeError) as exc:
        await invoke_node(flow.tool_chain, inputs={"tools": [calc]},
                          config={"steps": [{"tool": "calculator", "arguments": {"expression": "1/0"}}]})  # fmt: skip
    assert exc.value.code == "TOOL_CHAIN_FAILED"
    with pytest.raises(NodeError) as exc:
        bad_template = [{"tool": "calculator", "arguments": {"expression": "{{nope}}"}}]
        await invoke_node(flow.tool_chain, inputs={"tools": [calc]}, config={"steps": bad_template})
    assert exc.value.code == "TEMPLATE_MISSING_VALUE"


def test_render_keeps_types():
    scope = {"input": {"n": 3}, "previous": [1, 2], "steps": []}
    assert flow.render("{{input}}", scope) == {"n": 3}
    assert flow.render({"a": ["{{previous.1}}", "n={{input.n}}"]}, scope) == {"a": [2, "n=3"]}


# ---- iterative / ensemble / prompting methods -----------------------------------------------------
class Coach:
    """Scripted model for self_refine, reflexion, debate, mixture_of_agents, step_back and program_of_thought."""

    def __init__(self, expression="(1.10 - 1.00) / 2"):
        self.models: list[str] = []
        self.expression = expression
        self.attempts = 0

    async def chat(self, request, *, api_key=None, base_url=None):
        self.models.append(request.model)
        p = request.messages[0].content
        if "Answer it carefully" in p:
            return reply('{"answer": "0.10"}')
        if "Critique the answer" in p:
            done = "0.05" in p.split("Answer:")[1]
            return reply('{"issues": [], "good_enough": true}' if done else '{"issues": ["bat would cost 1.10"]}')
        if "Write the improved answer" in p:
            return reply('{"answer": "0.05"}')
        if "Solve it." in p:
            self.attempts += 1
            return reply(json.dumps({"reasoning": "r", "answer": "0.05" if "Lessons" in p else "0.10"}))
        if "Check it critically" in p:
            ok = "Proposed answer: 0.05" in p
            return reply(json.dumps({"correct": ok, "score": 9 if ok else 3, "feedback": "f"}))
        if "what should you do differently" in p:
            return reply('{"lesson": "check the difference constraint"}')
        if "Other agents said" in p:
            return reply('{"answer": "0.05", "why": "convinced"}')
        if "short justification" in p:
            return reply(json.dumps({"answer": "0.10" if request.model == "m2" else "0.05", "why": "w"}))
        if "Candidate answers from several models" in p:
            return reply('{"answer": "0.05 (synthesised)"}')
        if "Give your best answer" in p:
            return reply(json.dumps({"answer": f"0.05 from {request.model}"}))
        if "Step back" in p:
            return reply('{"principle": "set up two equations"}')
        if p.startswith("Principle:"):
            return reply('{"reasoning": "b - x = 1, b + x = 1.1", "answer": "0.05"}')
        if "ONE arithmetic expression" in p:
            expr = self.expression if "could not be evaluated" in p or self.expression.startswith("(") else "__import__"
            return reply(json.dumps({"reasoning": "x = (1.10 - 1.00)/2", "expression": expr, "unit": "dollars"}))
        return reply("{}")


async def test_self_refine_revises_until_the_critique_passes(monkeypatch):
    r = await think(monkeypatch, Coach(), method="self_refine", rounds=3)
    assert r.outputs["answer"] == "0.05"
    statuses = [n["status"] for n in r.outputs["result"]["tree"]]
    assert statuses.count("revised") == 1 and statuses[-1] == "chosen"


async def test_reflexion_retries_with_lessons(monkeypatch):
    coach = Coach()
    r = await think(monkeypatch, coach, method="reflexion", rounds=3)
    assert r.outputs["answer"] == "0.05" and coach.attempts == 2
    assert "check the difference constraint" in r.outputs["reasoning"]


async def test_debate_uses_each_model_and_converges(monkeypatch):
    coach = Coach()
    r = await think(monkeypatch, coach, method="debate", models=["m1", "m2", "m3"], rounds=2)
    assert r.outputs["answer"] == "0.05"
    assert {"m1", "m2", "m3"} <= set(coach.models)
    assert sum(1 for n in r.outputs["result"]["tree"] if n["status"] in ("revised", "chosen")) == 3


async def test_mixture_of_agents_aggregates_layers(monkeypatch):
    coach = Coach()
    r = await think(monkeypatch, coach, method="mixture_of_agents", agents=2, rounds=2)
    assert r.outputs["answer"] == "0.05 (synthesised)"
    layers = [n["status"] for n in r.outputs["result"]["tree"]]
    assert layers.count("layer 1") == 2 and layers.count("layer 2") == 2


async def test_step_back_names_the_principle_first(monkeypatch):
    r = await think(monkeypatch, Coach(), method="step_back")
    assert r.outputs["answer"] == "0.05" and r.outputs["reasoning"].startswith("Principle: set up two equations")


async def test_program_of_thought_evaluates_safely_and_retries(monkeypatch):
    r = await think(monkeypatch, Coach(), method="program_of_thought")
    assert r.outputs["answer"] == "0.05 dollars"
    bad_first = await think(monkeypatch, Coach(expression="0.1/2"), method="program_of_thought")
    assert bad_first.outputs["answer"] == "0.05 dollars"  # first expression rejected, then fixed
    assert "rejected" in {n["status"] for n in bad_first.outputs["result"]["tree"]}

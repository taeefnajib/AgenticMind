"""Reasoning node (ADR-0029): structured reasoning strategies over any chat model.

All methods share a call / token budget; when it runs out the search stops and the best path found so far
is answered from (``stopped_early`` in the result). Every explored thought is recorded in ``result.tree``
(id, parent(s), text, score, visits, status) so the trace shows *how* the answer was reached.

* ``chain_of_thought`` - step-by-step reasoning, then the answer. ``samples`` > 1 adds self-consistency:
  several independent chains at a higher temperature, majority vote on the answer.
* ``decomposition`` - task decomposition (least-to-most): split into sub-questions, answer them in order
  (each sees earlier answers), then compose the final answer.
* ``tree_of_thoughts`` - breadth-first beam search: each state proposes ``branching`` next steps, a scoring
  call rates them, the best ``beam_width`` survive, for up to ``depth`` levels (Yao et al., 2023).
* ``graph_of_thoughts`` - generate several candidate solutions, score them, aggregate the best into one
  (a node with several parents), then refine it while the score improves (Besta et al., 2023).
* ``mcts`` - Monte Carlo Tree Search over reasoning steps: UCT selection, model-proposed expansion, a
  model-estimated value as the simulation result, back-propagation; the path of best mean value wins.
* ``backtracking`` - depth-first search: propose ordered next steps, check each; a failed check moves to the
  next alternative, and a dead end returns to the previous step.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from agenticmind_node_sdk import (
    ExecutionPolicy,
    InputPort,
    ModelUsage,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    TransientNodeError,
    define_node,
    secret_field,
    ui,
)
from agenticmind_providers import (
    CHAT_PROVIDER_HELP,
    ChatMessage,
    ChatProviderName,
    ChatRequest,
    ProviderError,
    check_chat_provider,
    get_provider,
)
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from pydantic import Field, model_validator

from .tools import PACKAGE

Method = Literal["chain_of_thought", "decomposition", "tree_of_thoughts", "graph_of_thoughts", "mcts", "backtracking"]
SEARCH = ("tree_of_thoughts", "mcts", "backtracking")


def _on(*methods: str) -> dict[str, list[str]]:
    return {"method": list(methods)}


class ReasoningConfig(NodeConfig):
    method: Method = Field(
        default="chain_of_thought",
        description="chain_of_thought (optionally self-consistency), decomposition (sub-questions), "
        "tree_of_thoughts (beam search), graph_of_thoughts (generate, aggregate, refine), mcts (Monte Carlo "
        "tree search), backtracking (depth-first with checks).",
    )
    provider: ChatProviderName = Field(default="openrouter", description=CHAT_PROVIDER_HELP)
    model: str = Field(default=DEFAULT_OPENROUTER_MODEL, min_length=1)
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    instructions: str = Field(default="", description="Extra guidance for every step.",
                              json_schema_extra=ui(widget="textarea"))  # fmt: skip
    samples: int = Field(default=1, ge=1, le=9, description="Independent chains; >1 = self-consistency vote.",
                         json_schema_extra=ui(show_when=_on("chain_of_thought")))  # fmt: skip
    max_subquestions: int = Field(default=4, ge=1, le=10, json_schema_extra=ui(show_when=_on("decomposition")))
    branching: int = Field(default=3, ge=2, le=6, description="Candidate next steps proposed per state.",
                           json_schema_extra=ui(show_when=_on(*SEARCH, "graph_of_thoughts")))  # fmt: skip
    beam_width: int = Field(default=2, ge=1, le=5, description="States kept per level.",
                            json_schema_extra=ui(show_when=_on("tree_of_thoughts")))  # fmt: skip
    depth: int = Field(default=3, ge=1, le=8, description="Most reasoning steps per path.",
                       json_schema_extra=ui(show_when=_on(*SEARCH)))  # fmt: skip
    iterations: int = Field(default=8, ge=1, le=50, description="MCTS simulations.",
                            json_schema_extra=ui(show_when=_on("mcts")))  # fmt: skip
    refinements: int = Field(default=2, ge=0, le=5, json_schema_extra=ui(show_when=_on("graph_of_thoughts")))
    max_llm_calls: int = Field(default=40, ge=1, le=300, description="Stop searching after this many model calls.")
    token_budget: int = Field(default=80_000, ge=1_000, le=2_000_000)
    temperature: float = Field(default=0.7, ge=0, le=2, description="Used for proposing / sampling (scoring uses 0).",
                               json_schema_extra=ui(advanced=True))  # fmt: skip
    max_tokens: int = Field(
        default=1200, ge=64, le=100_000, description="Per call.", json_schema_extra=ui(advanced=True)
    )
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> ReasoningConfig:
        check_chat_provider(self.provider, self.api_key, self.base_url)
        return self


# ---- model access with a budget -----------------------------------------------------------------
class _OutOfBudget(Exception):
    pass


@dataclass
class _Thought:
    id: int
    text: str
    parents: list[int]
    score: float | None = None
    visits: int = 0
    value: float = 0.0
    final: bool = False
    status: str = "open"
    children: list[int] = field(default_factory=list)


class _Engine:
    def __init__(self, ctx: NodeContext, config: ReasoningConfig, question: str, context: str | None) -> None:
        self.ctx, self.config = ctx, config
        self.question = question
        self.context = context
        self.calls = self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.cost_known = False
        self.nodes: list[_Thought] = []
        self.provider = get_provider(config.provider)
        self.api_key = ctx.secret(config.api_key) if config.api_key else None

    # model -------------------------------------------------------------------------------------
    @property
    def system(self) -> str:
        base = "You are a rigorous problem solver. Follow the requested output format exactly."
        if self.config.instructions:
            base += "\n\n" + self.config.instructions
        if self.context:
            base += "\n\nContext:\n" + self.context
        return base

    async def ask(self, prompt: str, *, creative: bool = False, final: bool = False) -> str:
        """One model call. ``final`` (the answer call) may exceed the budget by one call, so a search that ran
        out still answers from its best path."""
        over = self.calls >= self.config.max_llm_calls or self.tokens_in + self.tokens_out >= self.config.token_budget
        if over and not final:
            raise _OutOfBudget
        self.calls += 1
        try:
            resp = await self.provider.chat(
                ChatRequest(
                    model=self.config.model,
                    system=self.system,
                    messages=[ChatMessage(role="user", content=prompt)],
                    temperature=self.config.temperature if creative else 0.0,
                    max_tokens=self.config.max_tokens,
                ),  # fmt: skip
                api_key=self.api_key,
                base_url=self.config.base_url,
            )
        except ProviderError as exc:
            if exc.retryable:
                raise TransientNodeError(exc.code, str(exc)) from exc
            raise NodeError(exc.code, str(exc)) from exc
        self.tokens_in += resp.usage.input_tokens
        self.tokens_out += resp.usage.output_tokens
        if resp.usage.estimated_cost_usd is not None:
            self.cost += resp.usage.estimated_cost_usd
            self.cost_known = True
        return resp.text or ""

    async def ask_json(self, prompt: str, *, creative: bool = False, final: bool = False) -> Any:
        return extract_json(await self.ask(prompt + "\n\nReply with JSON only.", creative=creative, final=final))

    # tree --------------------------------------------------------------------------------------
    def add(self, text: str, parents: list[int], **kw: Any) -> _Thought:
        node = _Thought(id=len(self.nodes), text=text.strip(), parents=parents, **kw)
        self.nodes.append(node)
        for p in parents:
            self.nodes[p].children.append(node.id)
        return node

    def path(self, node: _Thought) -> list[_Thought]:
        out = []
        while node.parents:
            out.append(node)
            node = self.nodes[node.parents[0]]
        return list(reversed(out))

    def steps_text(self, node: _Thought) -> str:
        steps = self.path(node)
        return "\n".join(f"Step {i}: {n.text}" for i, n in enumerate(steps, start=1)) or "(no steps yet)"

    async def emit(self, node: _Thought) -> None:
        if len(self.nodes) <= 60:  # keep traces readable; the full tree is in the result
            await self.ctx.emit("thought", {"id": node.id, "parents": node.parents, "score": node.score,
                                            "status": node.status, "text": node.text[:240]})  # fmt: skip

    # shared prompts -----------------------------------------------------------------------------
    async def propose(self, node: _Thought, k: int) -> list[tuple[str, bool]]:
        data = await self.ask_json(
            f"Problem: {self.question}\n\nReasoning so far:\n{self.steps_text(node)}\n\nPropose {k} different, "
            "promising next steps (each one short step, not a whole solution). Mark a step final if it states the "
            'answer to the problem.\nFormat: {"steps": [{"step": "...", "final": false}]}',
            creative=True,
        )
        items = data.get("steps") if isinstance(data, dict) else data
        out = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and str(item.get("step", "")).strip():
                out.append((str(item["step"]), bool(item.get("final"))))
            elif isinstance(item, str) and item.strip():
                out.append((item, False))
        return out[:k]

    async def score(self, parent: _Thought, candidates: list[str]) -> list[float]:
        listing = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, start=1))
        data = await self.ask_json(
            f"Problem: {self.question}\n\nReasoning so far:\n{self.steps_text(parent)}\n\nCandidate next steps:\n"
            f"{listing}\n\nRate each candidate from 0 (wrong or useless) to 10 (correct and clearly leads to the "
            'answer).\nFormat: {"scores": [7, 3]}'
        )
        raw = data.get("scores") if isinstance(data, dict) else data
        scores = [_num(x) for x in raw] if isinstance(raw, list) else []
        return (scores + [0.0] * len(candidates))[: len(candidates)]

    async def answer_from(self, node: _Thought, reasoning: str | None = None) -> str:
        data = await self.ask_json(
            f"Problem: {self.question}\n\nReasoning:\n{reasoning or self.steps_text(node)}\n\nGive the final "
            'answer to the problem based on this reasoning.\nFormat: {"answer": "..."}',
            final=True,
        )
        if isinstance(data, dict) and data.get("answer") is not None:
            return str(data["answer"]).strip()
        return node.text


def extract_json(text: str) -> Any:
    """The first JSON object or array in a reply (models sometimes wrap it in prose or code fences)."""
    text = re.sub(r"```(?:json)?", "", text)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    return {}


def _num(x: Any) -> float:
    try:
        return max(0.0, min(10.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _normalise(answer: str) -> str:
    return re.sub(r"[\s.,;:!?\"']+", " ", answer.lower()).strip()


# ---- methods ------------------------------------------------------------------------------------
async def chain_of_thought(e: _Engine) -> tuple[str, str]:
    root = e.add(e.question, [], status="root")
    prompt = (f"Problem: {e.question}\n\nThink step by step, then give the answer.\n"
              'Format: {"reasoning": "step-by-step reasoning", "answer": "the final answer"}')  # fmt: skip
    chains = []
    for _ in range(e.config.samples):
        try:
            data = await e.ask_json(prompt, creative=e.config.samples > 1)
        except _OutOfBudget:
            break
        if isinstance(data, dict) and data.get("answer") is not None:
            node = e.add(str(data.get("reasoning", "")), [root.id], status="sample")
            node.score = None
            chains.append((str(data["answer"]).strip(), node))
            await e.emit(node)
    if not chains:
        raise _OutOfBudget
    votes = Counter(_normalise(a) for a, _ in chains)
    winner, count = votes.most_common(1)[0]
    answer, node = next((a, n) for a, n in chains if _normalise(a) == winner)
    node.status = "chosen"
    if e.config.samples > 1:
        node.score = round(10 * count / len(chains), 1)
    return answer, node.text


async def decomposition(e: _Engine) -> tuple[str, str]:
    root = e.add(e.question, [], status="root")
    data = await e.ask_json(
        f"Problem: {e.question}\n\nBreak it into at most {e.config.max_subquestions} simpler sub-questions that, "
        'answered in order, solve it (earlier answers may be used by later ones).\nFormat: {"subquestions": ["..."]}'
    )
    subs = [str(s) for s in (data.get("subquestions") if isinstance(data, dict) else data) or [] if str(s).strip()]
    subs = subs[: e.config.max_subquestions] or [e.question]
    solved: list[tuple[str, str]] = []
    parent = root
    for q in subs:
        known = "\n".join(f"Q: {sq}\nA: {sa}" for sq, sa in solved) or "(none yet)"
        try:
            ans = await e.ask_json(f"Overall problem: {e.question}\n\nAlready answered:\n{known}\n\nNow answer: {q}\n"
                                   'Format: {"answer": "..."}')  # fmt: skip
        except _OutOfBudget:
            break
        a = str(ans.get("answer", "")) if isinstance(ans, dict) else str(ans)
        solved.append((q, a))
        parent = e.add(f"{q} → {a}", [parent.id], status="solved")
        await e.emit(parent)
    reasoning = "\n".join(f"{i}. {q} → {a}" for i, (q, a) in enumerate(solved, start=1))
    final = await e.ask_json(f"Problem: {e.question}\n\nSub-answers:\n{reasoning}\n\nCompose the final answer.\n"
                             'Format: {"answer": "..."}', final=True)  # fmt: skip
    parent.status = "chosen"
    return (str(final.get("answer", "")) if isinstance(final, dict) else str(final)).strip(), reasoning


async def tree_of_thoughts(e: _Engine) -> _Thought:
    root = e.add(e.question, [], status="root")
    frontier = [root]
    best: _Thought = root
    for _level in range(e.config.depth):
        scored: list[_Thought] = []
        for state in frontier:
            proposals = await e.propose(state, e.config.branching)
            if not proposals:
                continue
            scores = await e.score(state, [p for p, _ in proposals])
            for (text, final), s in zip(proposals, scores, strict=True):
                child = e.add(text, [state.id], score=s, final=final)
                scored.append(child)
                await e.emit(child)
        if not scored:
            break
        scored.sort(key=lambda n: -(n.score or 0))
        for pruned in scored[e.config.beam_width :]:
            pruned.status = "pruned"
        frontier = scored[: e.config.beam_width]
        best = max([best, *frontier], key=lambda n: n.score or 0)
        done = [n for n in frontier if n.final]
        if done:
            return max(done, key=lambda n: n.score or 0)
    return best


async def graph_of_thoughts(e: _Engine) -> _Thought:
    root = e.add(e.question, [], status="root")
    data = await e.ask_json(
        f"Problem: {e.question}\n\nWrite {e.config.branching} different complete solutions (different approaches)."
        '\nFormat: {"solutions": ["..."]}',
        creative=True,
    )
    sols = [str(s) for s in (data.get("solutions") if isinstance(data, dict) else data) or [] if str(s).strip()]
    nodes = [e.add(s, [root.id], status="generated") for s in sols[: e.config.branching]]
    if not nodes:
        return root
    for n, s in zip(nodes, await e.score(root, [n.text for n in nodes]), strict=True):
        n.score = s
        await e.emit(n)
    nodes.sort(key=lambda n: -(n.score or 0))
    best = nodes[0]
    top = [n for n in nodes[:3] if (n.score or 0) > 0] or nodes[:1]
    if len(top) > 1:
        try:
            merged = await e.ask_json(
                f"Problem: {e.question}\n\nCandidate solutions:\n"
                + "\n\n".join(f"[{i}] {n.text}" for i, n in enumerate(top, start=1))
                + "\n\nCombine their correct parts into one better solution."  # fmt: skip
                '\nFormat: {"solution": "..."}'
            )
            agg = e.add(str(merged.get("solution", "")) if isinstance(merged, dict) else "", [n.id for n in top],
                        status="aggregated")  # fmt: skip
            agg.score = (await e.score(root, [agg.text]))[0]
            await e.emit(agg)
            if (agg.score or 0) >= (best.score or 0):
                best = agg
        except _OutOfBudget:
            return best
    for _ in range(e.config.refinements):
        try:
            refined = await e.ask_json(
                f"Problem: {e.question}\n\nCurrent solution (score {best.score}/10):\n{best.text}\n\nFind its "
                'weaknesses and write an improved solution.\nFormat: {"solution": "..."}'
            )
            node = e.add(str(refined.get("solution", "")) if isinstance(refined, dict) else "", [best.id],
                         status="refined")  # fmt: skip
            node.score = (await e.score(root, [node.text]))[0]
            await e.emit(node)
        except _OutOfBudget:
            break
        if (node.score or 0) <= (best.score or 0):
            node.status = "rejected"
            break
        best = node
    return best


async def mcts(e: _Engine, c: float = 1.4) -> _Thought:
    root = e.add(e.question, [], status="root")
    for _ in range(e.config.iterations):
        # selection
        node = root
        while node.children and not node.final:
            parent_visits = max(1, node.visits)
            node = max(
                (e.nodes[i] for i in node.children),
                key=lambda ch: float("inf") if ch.visits == 0 else
                ch.value / ch.visits + c * math.sqrt(math.log(parent_visits) / ch.visits),
            )  # fmt: skip
            if node.visits == 0:
                break
        # expansion
        if node.visits > 0 and not node.final and len(e.path(node)) < e.config.depth:
            proposals = await e.propose(node, e.config.branching)
            for text, final in proposals:
                e.add(text, [node.id], final=final)
            if node.children:
                node = e.nodes[node.children[0]]
        # simulation: the model estimates how likely this partial reasoning leads to a correct answer
        data = await e.ask_json(
            f"Problem: {e.question}\n\nPartial reasoning:\n{e.steps_text(node)}\n\nHow likely is this reasoning to "
            'lead to a correct answer? Give a probability between 0 and 1.\nFormat: {"value": 0.6}'
        )
        value = max(0.0, min(1.0, _num(data.get("value") if isinstance(data, dict) else data)))
        node.score = round(value * 10, 2)
        # back-propagation
        walker: _Thought | None = node
        while walker is not None:
            walker.visits += 1
            walker.value += value
            walker = e.nodes[walker.parents[0]] if walker.parents else None
        await e.emit(node)
    # follow the best explored path: highest mean value among visited children (ties: more visits)
    node = root
    while True:
        visited = [e.nodes[i] for i in node.children if e.nodes[i].visits > 0]
        if not visited:
            return node
        node = max(visited, key=lambda ch: (ch.value / ch.visits, ch.visits))
        node.status = "chosen"


async def backtracking(e: _Engine) -> _Thought:
    root = e.add(e.question, [], status="root")
    best = root

    async def check(parent: _Thought, step: str) -> tuple[bool, str]:
        data = await e.ask_json(
            f"Problem: {e.question}\n\nReasoning so far:\n{e.steps_text(parent)}\n\nProposed next step: {step}\n\n"
            'Is this step correct and useful? Format: {"valid": true, "reason": "..."}'
        )
        if isinstance(data, dict):
            return bool(data.get("valid")), str(data.get("reason", ""))
        return False, "no verdict"

    async def dfs(state: _Thought) -> _Thought | None:
        nonlocal best
        if len(e.path(state)) >= e.config.depth:
            return None
        for text, final in await e.propose(state, e.config.branching):
            ok, reason = await check(state, text)
            child = e.add(text, [state.id], final=final, status="valid" if ok else "rejected")
            child.score = 10.0 if ok else 0.0
            await e.emit(child)
            if not ok:
                continue  # try the next alternative
            if len(e.path(child)) > len(e.path(best)):
                best = child
            if final:
                return child
            found = await dfs(child)
            if found:
                return found
            child.status = "dead_end"  # every continuation failed: back up
        return None

    found = await dfs(root)
    return found or best


# ---- node ---------------------------------------------------------------------------------------
@define_node(
    type="agenticmind.agents.reasoning",
    version="1.0.0",
    display_name="Reasoning",
    category="agents",
    package=PACKAGE,
    description="Answers with a reasoning strategy: chain of thought (with self-consistency), task decomposition, "
    "tree of thoughts, graph of thoughts, Monte Carlo tree search or backtracking. The explored tree is traced.",
    inputs=[
        InputPort(name="question", type=PortType.STRING),
        InputPort(name="context", type=PortType.STRING, required=False, description="Facts the reasoning may use"),
    ],
    outputs=[
        OutputPort(name="answer", type=PortType.STRING),
        OutputPort(name="reasoning", type=PortType.STRING, description="The chosen reasoning path"),
        OutputPort(name="result", type=PortType.JSON, description="{method, answer, path, tree, calls, stopped_early}"),
    ],
    config=ReasoningConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=900, max_attempts=1, idempotent=True),
    tags=["agents", "reasoning"],
)
async def reasoning(ctx: NodeContext, inputs, config: ReasoningConfig) -> NodeResult:
    question = inputs["question"].strip()
    if not question:
        raise NodeError("QUESTION_EMPTY", "The question is empty")
    e = _Engine(ctx, config, question, inputs.get("context"))
    stopped = False
    try:
        if config.method in ("chain_of_thought", "decomposition"):
            runner = chain_of_thought if config.method == "chain_of_thought" else decomposition
            answer, chain = await runner(e)
        else:
            search = {"tree_of_thoughts": tree_of_thoughts, "graph_of_thoughts": graph_of_thoughts, "mcts": mcts,
                      "backtracking": backtracking}[config.method]  # fmt: skip
            try:
                best = await search(e)
            except _OutOfBudget:
                stopped = True
                scored = [n for n in e.nodes if n.parents and n.status not in ("rejected", "pruned")]
                best = max(scored, key=lambda n: n.score or 0) if scored else e.nodes[0]
            best.status = "chosen"
            # graph of thoughts keeps whole solutions in its nodes; the searches keep one step per node
            chain = best.text if config.method == "graph_of_thoughts" else e.steps_text(best)
            answer = await e.answer_from(best, chain)
    except _OutOfBudget as exc:
        raise NodeError("REASONING_BUDGET", "The call or token budget ran out before any answer was found; raise "
                        "max_llm_calls / token_budget") from exc  # fmt: skip
    await ctx.emit("reasoning_done", {"method": config.method, "calls": e.calls, "thoughts": len(e.nodes),
                                      "stopped_early": stopped})  # fmt: skip
    tree = [
        {"id": n.id, "parents": n.parents, "text": n.text, "score": n.score, "visits": n.visits or None,
         "status": n.status, "final": n.final or None}
        for n in e.nodes
    ]  # fmt: skip
    tree = [{k: v for k, v in n.items() if v is not None} for n in tree]
    return NodeResult(
        outputs={
            "answer": answer,
            "reasoning": chain,
            "result": {"method": config.method, "answer": answer, "reasoning": chain, "tree": tree,
                       "calls": e.calls, "stopped_early": stopped},  # fmt: skip
        },
        usage=ModelUsage(provider=config.provider, model=config.model, input_tokens=e.tokens_in,
                         output_tokens=e.tokens_out, estimated_cost_usd=round(e.cost, 6) if e.cost_known else None),
    )  # fmt: skip

"""Versioned evaluators (blueprint §10). Each declares its area, what it needs and how it aggregates.

Deterministic evaluators are exact; LLM-judge evaluators are estimates and record their judge model,
prompt version and rubric with every score.
"""

from __future__ import annotations

import math
import re
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from .models import MetricResult, Observation, TestCase

Area = Literal["retrieval", "generation", "safety", "operations", "agent"]


@dataclass(frozen=True)
class EvaluatorSpec:
    name: str
    version: str
    area: Area
    title: str
    description: str
    requires: tuple[str, ...]  # case fields / observation parts it needs
    higher_is_better: bool = True
    uses_judge: bool = False
    aggregation: str = "mean over applicable cases"
    unit: str = ""


@dataclass
class Context:
    k: int
    judge: Any | None = None  # judge.Judge


EvalFn = Callable[[TestCase, Observation, Context], Awaitable[MetricResult]]
REGISTRY: dict[str, tuple[EvaluatorSpec, EvalFn]] = {}


def evaluator(spec: EvaluatorSpec) -> Callable[[EvalFn], EvalFn]:
    def deco(fn: EvalFn) -> EvalFn:
        REGISTRY[spec.name] = (spec, fn)
        return fn

    return deco


def catalogue() -> list[EvaluatorSpec]:
    return [spec for spec, _ in REGISTRY.values()]


def _na(spec: EvaluatorSpec, reason: str) -> MetricResult:
    return MetricResult(name=spec.name, version=spec.version, value=None, details={"not_applicable": reason})


def _ok(spec: EvaluatorSpec, value: float, passed: bool | None = None, **details: Any) -> MetricResult:
    return MetricResult(name=spec.name, version=spec.version, value=round(value, 6), passed=passed, details=details)


# ---- retrieval ----------------------------------------------------------------------------------
def relevance(case: TestCase, docs: list[dict[str, Any]]) -> list[bool]:
    return [any(e.matches(d) for e in case.expected_sources) for d in docs]


def _retrieval(case: TestCase, obs: Observation) -> str | None:
    if not case.expected_sources:
        return "case has no expected_sources"
    if obs.retrieval_node is None:
        return "graph has no retrieval node"
    return None


R = "retrieval"
RECALL = EvaluatorSpec(
    "recall_at_k",
    "1.0",
    R,
    "Recall@K",
    "Share of expected sources found in the top K results.",
    ("expected_sources", "retrieval"),
)
PRECISION = EvaluatorSpec(
    "precision_at_k",
    "1.0",
    R,
    "Precision@K",
    "Share of the top K results (or fewer, if fewer were returned) that match an expected source.",
    ("expected_sources", "retrieval"),
)
MRR = EvaluatorSpec(
    "mrr",
    "1.0",
    R,
    "MRR",
    "1 / rank of the first relevant result (0 if none in the top K).",
    ("expected_sources", "retrieval"),
)
NDCG = EvaluatorSpec(
    "ndcg_at_k",
    "1.0",
    R,
    "NDCG@K",
    "Ranking quality with binary relevance; 1 = all expected sources ranked first.",
    ("expected_sources", "retrieval"),
)
HIT = EvaluatorSpec(
    "hit_rate",
    "1.0",
    R,
    "Hit rate",
    "1 if any relevant result is in the top K.",
    ("expected_sources", "retrieval"),
    aggregation="share of applicable cases",
)


@evaluator(RECALL)
async def recall_at_k(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if reason := _retrieval(case, obs):
        return _na(RECALL, reason)
    top = obs.retrieved[: ctx.k]
    found = [e.label() for e in case.expected_sources if any(e.matches(d) for d in top)]
    return _ok(RECALL, len(found) / len(case.expected_sources), found=found, k=ctx.k)


@evaluator(PRECISION)
async def precision_at_k(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if reason := _retrieval(case, obs):
        return _na(PRECISION, reason)
    rel = relevance(case, obs.retrieved[: ctx.k])
    if not rel:
        return _ok(PRECISION, 0.0, k=ctx.k, retrieved=0)
    return _ok(PRECISION, sum(rel) / len(rel), k=ctx.k, retrieved=len(rel))


@evaluator(MRR)
async def mrr(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if reason := _retrieval(case, obs):
        return _na(MRR, reason)
    rel = relevance(case, obs.retrieved[: ctx.k])
    rank = next((i + 1 for i, r in enumerate(rel) if r), None)
    return _ok(MRR, 1 / rank if rank else 0.0, first_relevant_rank=rank)


@evaluator(NDCG)
async def ndcg_at_k(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if reason := _retrieval(case, obs):
        return _na(NDCG, reason)
    rel = relevance(case, obs.retrieved[: ctx.k])
    dcg = sum(1 / math.log2(i + 2) for i, r in enumerate(rel) if r)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(ctx.k, len(case.expected_sources))))
    return _ok(NDCG, dcg / ideal if ideal else 0.0, k=ctx.k)


@evaluator(HIT)
async def hit_rate(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if reason := _retrieval(case, obs):
        return _na(HIT, reason)
    hit = any(relevance(case, obs.retrieved[: ctx.k]))
    return _ok(HIT, 1.0 if hit else 0.0, passed=hit)


# ---- generation (deterministic) -----------------------------------------------------------------
G = "generation"
CONTAINS = EvaluatorSpec(
    "contains_required",
    "1.0",
    G,
    "Required facts",
    "Share of must_include phrases present in the answer (case-insensitive).",
    ("must_include", "answer"),
)
F1 = EvaluatorSpec(
    "reference_f1",
    "1.0",
    G,
    "Token F1 vs reference",
    "Word-overlap F1 between answer and reference (SQuAD-style normalisation). Rewards wording, not meaning.",
    ("reference", "answer"),
)
CITE_VALID = EvaluatorSpec(
    "citation_validity",
    "1.1",  # 1.1: an answer that cites nothing is not applicable when the case expects no sources
    G,
    "Citation validity",
    "Share of citation markers that point to a provided source. Citing nothing counts as 0, unless the case "
    "expects no sources (e.g. an unanswerable question), where it is not applicable.",
    ("citations",),
)
CITE_COVER = EvaluatorSpec(
    "citation_source_recall",
    "1.0",
    G,
    "Cited expected sources",
    "Share of expected sources that the answer cites.",
    ("expected_sources", "citations"),
)

_ARTICLES = re.compile(r"\b(a|an|the)\b")


def normalize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"\[\d+(?:[-,]\s*\d+)*\]", " ", text)  # citation markers
    text = "".join(ch if ch not in string.punctuation else " " for ch in text)
    return _ARTICLES.sub(" ", text).split()


def token_f1(answer: str, reference: str) -> float:
    a, r = normalize(answer), normalize(reference)
    if not a or not r:
        return float(a == r)
    common = sum(min(a.count(t), r.count(t)) for t in set(a))
    if not common:
        return 0.0
    precision, recall = common / len(a), common / len(r)
    return 2 * precision * recall / (precision + recall)


@evaluator(CONTAINS)
async def contains_required(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if not case.must_include:
        return _na(CONTAINS, "case has no must_include phrases")
    if obs.answer is None:
        return _ok(CONTAINS, 0.0, passed=False, missing=case.must_include, note="no answer")
    low = obs.answer.lower()
    missing = [p for p in case.must_include if p.lower() not in low]
    share = 1 - len(missing) / len(case.must_include)
    return _ok(CONTAINS, share, passed=not missing, missing=missing)


@evaluator(F1)
async def reference_f1(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if not case.reference:
        return _na(F1, "case has no reference answer")
    return _ok(F1, token_f1(obs.answer or "", case.reference))


@evaluator(CITE_VALID)
async def citation_validity(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    total = len(obs.citations) + len(obs.invalid_citations)
    if obs.answer is None:
        return _na(CITE_VALID, "no answer")
    if total == 0:
        if not case.expected_sources:
            return _na(CITE_VALID, "nothing to cite: the case expects no sources")
        return _ok(CITE_VALID, 0.0, passed=False, note="the answer cites nothing")
    return _ok(CITE_VALID, len(obs.citations) / total, passed=not obs.invalid_citations, invalid=obs.invalid_citations)


@evaluator(CITE_COVER)
async def citation_source_recall(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if not case.expected_sources:
        return _na(CITE_COVER, "case has no expected_sources")
    cited = [e.label() for e in case.expected_sources if any(e.matches(c) for c in obs.citations)]
    return _ok(CITE_COVER, len(cited) / len(case.expected_sources), cited=cited)


# ---- safety -------------------------------------------------------------------------------------
FORBIDDEN = EvaluatorSpec(
    "forbidden_content",
    "1.0",
    "safety",
    "No forbidden content",
    "1 if the answer contains none of the must_not_include phrases.",
    ("must_not_include", "answer"),
    aggregation="share of applicable cases",
)


@evaluator(FORBIDDEN)
async def forbidden_content(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if not case.must_not_include:
        return _na(FORBIDDEN, "case has no must_not_include phrases")
    low = (obs.answer or "").lower()
    found = [p for p in case.must_not_include if p.lower() in low]
    return _ok(FORBIDDEN, 0.0 if found else 1.0, passed=not found, found=found)


# ---- agent --------------------------------------------------------------------------------------
TOOLS = EvaluatorSpec(
    "tool_use",
    "1.0",
    "agent",
    "Expected tools used",
    "Share of expected_tools the agent called (including inside subagents).",
    ("expected_tools",),
)


@evaluator(TOOLS)
async def tool_use(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if not case.expected_tools:
        return _na(TOOLS, "case has no expected_tools")
    used = set(obs.tools_used)
    missing = [t for t in case.expected_tools if t not in used]
    return _ok(
        TOOLS, 1 - len(missing) / len(case.expected_tools), passed=not missing, missing=missing, used=sorted(used)
    )


# ---- operations ---------------------------------------------------------------------------------
OPS = "operations"
SUCCESS = EvaluatorSpec(
    "success",
    "1.0",
    OPS,
    "Run succeeded",
    "1 if the run finished without error.",
    (),
    aggregation="share of cases (1 - failure rate)",
)
LATENCY = EvaluatorSpec(
    "latency_ms",
    "1.0",
    OPS,
    "Latency",
    "Wall-clock duration of the run.",
    (),
    higher_is_better=False,
    aggregation="mean, p50 and p95",
    unit="ms",
)
TOKENS = EvaluatorSpec(
    "tokens",
    "1.0",
    OPS,
    "Tokens",
    "Input + output tokens reported by model providers.",
    (),
    higher_is_better=False,
    aggregation="mean and total",
)
COST = EvaluatorSpec(
    "cost_usd",
    "1.0",
    OPS,
    "Estimated cost",
    "Provider-reported cost (only when providers report it).",
    (),
    higher_is_better=False,
    aggregation="mean and total",
    unit="USD",
)
RETRIES = EvaluatorSpec(
    "retries",
    "1.0",
    OPS,
    "Retries",
    "Node retry attempts during the run.",
    (),
    higher_is_better=False,
    aggregation="mean and total",
)


@evaluator(SUCCESS)
async def success(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    ok = obs.status == "succeeded"
    return _ok(SUCCESS, 1.0 if ok else 0.0, passed=ok, **({"error": obs.error} if obs.error else {}))


@evaluator(LATENCY)
async def latency_ms(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    return _ok(LATENCY, obs.latency_ms)


@evaluator(TOKENS)
async def tokens(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    return _ok(TOKENS, float(obs.input_tokens + obs.output_tokens), input=obs.input_tokens, output=obs.output_tokens)


@evaluator(COST)
async def cost_usd(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if obs.cost_usd is None:
        return _na(COST, "no provider reported a cost")
    return _ok(COST, obs.cost_usd)


@evaluator(RETRIES)
async def retries(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    return _ok(RETRIES, float(obs.retries))


# ---- LLM judge (estimates) ----------------------------------------------------------------------
CORRECT = EvaluatorSpec(
    "correctness_judge",
    "1.0",
    G,
    "Correctness (LLM judge)",
    "A judge model compares the answer with the reference (1-5, scaled to 0-1). An estimate, not ground truth.",
    ("reference", "answer", "judge"),
    uses_judge=True,
)
GROUNDED = EvaluatorSpec(
    "groundedness_judge",
    "1.0",
    G,
    "Groundedness (LLM judge)",
    "A judge model checks that every claim is supported by the retrieved passages (1-5, scaled to 0-1). "
    "Grounded is not the same as correct.",
    ("answer", "retrieval", "judge"),
    uses_judge=True,
)


@evaluator(CORRECT)
async def correctness_judge(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if not case.reference:
        return _na(CORRECT, "case has no reference answer")
    if ctx.judge is None:
        return _na(CORRECT, "no judge configured")
    if not obs.answer:
        return _ok(CORRECT, 0.0, passed=False, note="no answer")
    grade = await ctx.judge.correctness(case, obs.answer)
    return _ok(CORRECT, (grade["score"] - 1) / 4, passed=grade["score"] >= 4, **grade)


@evaluator(GROUNDED)
async def groundedness_judge(case: TestCase, obs: Observation, ctx: Context) -> MetricResult:
    if ctx.judge is None:
        return _na(GROUNDED, "no judge configured")
    if not obs.answer:
        return _na(GROUNDED, "no answer")
    if not obs.retrieved:
        return _na(GROUNDED, "no retrieved passages to check against")
    grade = await ctx.judge.groundedness(case, obs.answer, obs.retrieved[: ctx.k])
    return _ok(GROUNDED, (grade["score"] - 1) / 4, passed=grade["score"] >= 4, **grade)

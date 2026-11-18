"""Run a dataset through a project graph, score each case and aggregate (ADR-0017).

Every case is a normal runtime execution with its own run ID and trace, so per-case results stay auditable.
Only the part of the graph the answer depends on is executed (run target = the answer Output node).
"""

from __future__ import annotations

import asyncio
import inspect
import statistics
from collections.abc import Mapping
from typing import Any

from agenticmind_ir import ProjectDocument
from agenticmind_registry import NodeRegistry
from agenticmind_runtime import (
    CancelToken,
    Engine,
    EventType,
    InMemoryObserver,
    PlanError,
    RunResult,
    SecretResolver,
    TraceEvent,
    compile_plan,
    new_id,
)

from .evaluators import REGISTRY, Context, EvaluatorSpec
from .judge import Judge, JudgeError
from .models import CaseResult, DatasetContent, EvaluationConfig, MetricResult, Observation, TestCase

OUTPUT_TYPE = "agenticmind.core.output"
#: Nodes whose ``documents`` output is what gets scored by retrieval metrics (latest in the plan wins).
RETRIEVAL_TYPES = (
    "agenticmind.rag.retriever",
    "agenticmind.rag.keyword_search",
    "agenticmind.rag.hybrid_search",
    "agenticmind.rag.reranker",
    "agenticmind.rag.context_compressor",
)


class EvaluationError(Exception):
    def __init__(self, message: str, diagnostics: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


def answer_output(doc: ProjectDocument, wanted: str | None) -> tuple[str, str]:
    """(node id, output key) of the Output node whose value is scored as the answer."""
    outs = {n.id: str(n.config.get("key", n.id)) for n in doc.nodes if n.type == OUTPUT_TYPE}
    if not outs:
        raise EvaluationError("The graph has no Output node to evaluate")
    if wanted:
        for node_id, key in outs.items():
            if wanted in (node_id, key):
                return node_id, key
        raise EvaluationError(f"No Output node or key '{wanted}'")
    for node_id, key in sorted(outs.items()):
        if key == "answer":
            return node_id, key
    node_id = sorted(outs)[0]
    return node_id, outs[node_id]


class _Capture:
    """Wraps another observer and keeps events and node outputs for scoring."""

    def __init__(self, inner: Any | None) -> None:
        self.inner = inner
        self.events: list[TraceEvent] = []
        self.outputs: dict[str, dict[str, Any]] = {}

    async def on_event(self, event: TraceEvent) -> None:
        self.events.append(event)
        if self.inner is not None:
            await self.inner.on_event(event)

    async def on_node_outputs(self, run_id: str, node_id: str, invocation_id: str, outputs: Mapping[str, Any]) -> None:
        self.outputs[node_id] = dict(outputs)
        if self.inner is not None:
            await self.inner.on_node_outputs(run_id, node_id, invocation_id, outputs)

    async def on_checkpoint(self, run_id: str, checkpoint: Any) -> None:
        if self.inner is not None and hasattr(self.inner, "on_checkpoint"):
            await self.inner.on_checkpoint(run_id, checkpoint)


def _tools(steps: Any) -> list[str]:
    out: list[str] = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            if step.get("tool"):
                out.append(str(step["tool"]))
            out += _tools(step.get("substeps"))
    return out


def observe(result: RunResult, capture: _Capture, answer_key: str, retrieval_node: str | None) -> Observation:
    value = result.outputs.get(answer_key)
    answer: str | None = None
    citations: list[dict[str, Any]] = []
    invalid: list[int] = []
    tools: list[str] = []
    if isinstance(value, str):
        answer = value
    elif isinstance(value, dict) and isinstance(value.get("answer"), str):
        answer = value["answer"]
        citations = [c for c in value.get("citations") or [] if isinstance(c, dict)]
        invalid = [int(i) for i in (value.get("validation") or {}).get("invalid", []) if isinstance(i, int)]
        tools = _tools(value.get("steps"))
    elif value is not None:
        answer = str(value)
    input_tokens = output_tokens = retries = 0
    cost: float | None = None
    for e in capture.events:
        if e.event_type == EventType.NODE_RETRYING:
            retries += 1
        if e.usage is not None and e.event_type == EventType.NODE_SUCCEEDED:
            input_tokens += e.usage.input_tokens
            output_tokens += e.usage.output_tokens
            if e.usage.estimated_cost_usd is not None:
                cost = (cost or 0.0) + e.usage.estimated_cost_usd
    retrieved = []
    if retrieval_node and isinstance(capture.outputs.get(retrieval_node, {}).get("documents"), list):
        retrieved = [d for d in capture.outputs[retrieval_node]["documents"] if isinstance(d, dict)]
    return Observation(
        run_id=result.run_id,
        status=result.status.value,
        error=result.error.model_dump() if result.error else None,
        answer=answer,
        citations=citations,
        invalid_citations=invalid,
        retrieved=retrieved,
        retrieval_node=retrieval_node,
        tools_used=tools,
        outputs={k: v for k, v in result.outputs.items()},
        latency_ms=round((result.finished_at - result.started_at).total_seconds() * 1000, 1),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost, 6) if cost is not None else None,
        retries=retries,
    )


async def _maybe(fn: Any, *args: Any) -> Any:
    if fn is None:
        return None
    out = fn(*args)
    return await out if inspect.isawaitable(out) else out


async def run_evaluation(
    doc: ProjectDocument,
    registry: NodeRegistry,
    dataset: DatasetContent,
    config: EvaluationConfig,
    *,
    secrets: SecretResolver | None = None,
    engine_kwargs: dict[str, Any] | None = None,
    hooks: Any = None,
    judge_api_key: str | None = None,
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Execute and score every case. ``hooks`` may define ``observer(run_id, case)``, ``started(run_id, case)``,
    ``finished(result)`` and ``case_done(case_result)`` (sync or async) to persist as it goes."""
    unknown = [e for e in config.evaluators if e not in REGISTRY]
    if unknown:
        raise EvaluationError(f"Unknown evaluators: {', '.join(unknown)}")
    answer_id, answer_key = answer_output(doc, config.answer_output)
    try:
        plan = compile_plan(doc, registry, secrets=secrets, targets=[answer_id])
    except PlanError as exc:
        raise EvaluationError("The graph is not executable", exc.report.to_public()["diagnostics"]) from exc
    if config.retrieval_node:
        if config.retrieval_node not in plan.nodes:
            raise EvaluationError(f"'{config.retrieval_node}' is not part of what the answer depends on")
        retrieval_node: str | None = config.retrieval_node
    else:
        candidates = [n for n in plan.order if plan.nodes[n].node.type in RETRIEVAL_TYPES]
        retrieval_node = candidates[-1] if candidates else None
    specs = [REGISTRY[name][0] for name in config.evaluators]
    judge = Judge(config.judge, judge_api_key) if config.judge and any(s.uses_judge for s in specs) else None
    ctx = Context(k=config.k, judge=judge)
    semaphore = asyncio.Semaphore(config.concurrency)
    token = cancel or CancelToken()

    async def run_case(case: TestCase) -> CaseResult:
        async with semaphore:
            run_id = new_id("run")
            inner = await _maybe(getattr(hooks, "observer", None), run_id, case) or InMemoryObserver()
            capture = _Capture(inner)
            await _maybe(getattr(hooks, "started", None), run_id, case)
            engine = Engine(observer=capture, secrets=secrets, **(engine_kwargs or {}))
            result = await engine.run(plan, run_id=run_id, inputs=dict(case.inputs), cancel=token)
            await _maybe(getattr(hooks, "finished", None), result)
            obs = observe(result, capture, answer_key, retrieval_node)
            metrics: list[MetricResult] = []
            for name in config.evaluators:
                spec, fn = REGISTRY[name]
                try:
                    metrics.append(await fn(case, obs, ctx))
                except JudgeError as exc:
                    metrics.append(
                        MetricResult(name=spec.name, version=spec.version, value=None, details={"error": str(exc)})
                    )
            case_result = CaseResult(
                case_id=case.id, run_id=run_id, status=obs.status, error=obs.error, metrics=metrics, observation=obs
            )
            await _maybe(getattr(hooks, "case_done", None), case_result)
            return case_result

    results = await asyncio.gather(*(run_case(c) for c in dataset.cases))
    return {
        "cases": results,
        "summary": summarize(results, specs),
        "setup": {
            "answer_output": answer_key,
            "answer_node": answer_id,
            "retrieval_node": retrieval_node,
            "k": config.k,
            "judge": judge.describe() if judge else None,
            "judge_usage": (
                {
                    "input_tokens": judge.input_tokens,
                    "output_tokens": judge.output_tokens,
                    "cost_usd": round(judge.cost_usd, 6),
                }
                if judge
                else None
            ),
            "dataset_hash": dataset.content_hash(),
        },
    }


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return ordered[idx]


def summarize(results: list[CaseResult], specs: list[EvaluatorSpec]) -> list[dict[str, Any]]:
    out = []
    for spec in specs:
        values = [m.value for r in results if (m := r.metric(spec.name)) is not None and m.value is not None]
        passed = [m.passed for r in results if (m := r.metric(spec.name)) is not None and m.passed is not None]
        row: dict[str, Any] = {
            "name": spec.name,
            "version": spec.version,
            "area": spec.area,
            "title": spec.title,
            "higher_is_better": spec.higher_is_better,
            "estimate": spec.uses_judge,
            "aggregation": spec.aggregation,
            "unit": spec.unit,
            "n": len(values),
            "of": len(results),
            "mean": round(statistics.fmean(values), 6) if values else None,
            "passed": sum(1 for p in passed if p) if passed else None,
        }
        if spec.name == "latency_ms" and values:
            row["p50"] = _percentile(values, 0.5)
            row["p95"] = _percentile(values, 0.95)
        if spec.name in ("tokens", "cost_usd", "retries") and values:
            row["total"] = round(sum(values), 6)
        out.append(row)
    return out


def compare(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Compare two evaluation runs (each: {summary, cases}). Per-case diffs cover cases present in both."""
    sa = {m["name"]: m for m in a["summary"]}
    sb = {m["name"]: m for m in b["summary"]}
    metrics = []
    for name in [n for n in sa if n in sb]:
        ma, mb = sa[name], sb[name]
        delta = None if ma["mean"] is None or mb["mean"] is None else round(mb["mean"] - ma["mean"], 6)
        better = None if delta is None or abs(delta) < 1e-9 else (delta > 0) == ma["higher_is_better"]
        metrics.append(
            {
                **{k: ma[k] for k in ("name", "title", "area", "higher_is_better", "estimate", "unit")},
                "a": ma["mean"],
                "b": mb["mean"],
                "n_a": ma["n"],
                "n_b": mb["n"],
                "delta": delta,
                "better": better,
            }
        )
    by_a = {c["case_id"]: c for c in a["cases"]}
    by_b = {c["case_id"]: c for c in b["cases"]}
    cases = []
    regressions = improvements = 0
    hib = {m["name"]: m["higher_is_better"] for m in metrics}
    for case_id in [c for c in by_a if c in by_b]:
        ca = {m["name"]: m for m in by_a[case_id]["metrics"]}
        cb = {m["name"]: m for m in by_b[case_id]["metrics"]}
        changes = []
        for name, higher in hib.items():
            va = ca.get(name, {}).get("value")
            vb = cb.get(name, {}).get("value")
            if va is None or vb is None or abs(vb - va) < 1e-9 or name in ("latency_ms", "tokens", "cost_usd"):
                continue
            better = (vb > va) == higher
            changes.append({"metric": name, "a": va, "b": vb, "better": better})
        regressed = any(not c["better"] for c in changes)
        improved = any(c["better"] for c in changes)
        regressions += regressed
        improvements += improved
        cases.append(
            {
                "case_id": case_id,
                "run_a": by_a[case_id]["run_id"],
                "run_b": by_b[case_id]["run_id"],
                "changes": changes,
                "regressed": regressed,
                "improved": improved,
            }
        )
    return {
        "metrics": metrics,
        "cases": cases,
        "regressions": regressions,
        "improvements": improvements,
        "only_in_a": sorted(set(by_a) - set(by_b)),
        "only_in_b": sorted(set(by_b) - set(by_a)),
        "note": "Quality metrics are compared per case; latency, tokens and cost are compared in aggregate only.",
    }

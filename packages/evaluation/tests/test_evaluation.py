"""Evaluation Lab core: metrics, judge, runner (through the real engine) and comparison."""

import importlib

import pytest
from agenticmind_evaluation import (
    DatasetContent,
    EvaluationConfig,
    EvaluationError,
    ExpectedSource,
    JudgeConfig,
    Observation,
    TestCase,
    compare,
    run_evaluation,
)
from agenticmind_evaluation.evaluators import REGISTRY, Context, token_f1
from agenticmind_ir import load_project
from agenticmind_node_sdk import ModelUsage
from agenticmind_providers import ChatResponse, ToolCall
from agenticmind_registry import default_registry
from agenticmind_runtime import Engine, InMemoryObserver, LocalArtifactStore, compile_plan

judge_mod = importlib.import_module("agenticmind_evaluation.judge")

DOCS = [
    {
        "id": "c1",
        "content": "Travel: hotels up to 180 EUR.",
        "source": {"uri": "x", "filename": "hb.md", "section": "Handbook > Travel"},
    },
    {
        "id": "c2",
        "content": "Annual leave: 25 days.",
        "source": {"uri": "x", "filename": "hb.md", "section": "Handbook > Annual leave"},
    },
    {
        "id": "c3",
        "content": "Security: report phishing.",
        "source": {"uri": "x", "filename": "hb.md", "section": "Handbook > Security"},
    },
]


def obs(**kw):
    return Observation(run_id="r", status="succeeded", retrieval_node="retrieve", **kw)


async def score(name, case, observation, k=3, judge=None):
    return await REGISTRY[name][1](case, observation, Context(k=k, judge=judge))


def test_expected_source_parsing_and_matching():
    assert ExpectedSource.parse("hb.md#annual leave") == ExpectedSource(filename="hb.md", section="annual leave")
    assert ExpectedSource.parse("hb.md") == ExpectedSource(filename="hb.md")
    assert ExpectedSource.parse("art_0123456789abcdef#c2") == ExpectedSource(id="art_0123456789abcdef#c2")
    assert ExpectedSource.parse("c2") == ExpectedSource(id="c2")
    e = ExpectedSource(filename="HB.md", section="annual")
    assert e.matches(DOCS[1]) and not e.matches(DOCS[0])
    citation = {"n": 1, "id": "c2", "filename": "hb.md", "section": "Handbook > Annual leave"}  # flat citation shape
    assert e.matches(citation)


async def test_retrieval_metrics():
    case = TestCase(id="q", expected_sources=["hb.md#annual leave"])
    o = obs(retrieved=DOCS)
    assert (await score("recall_at_k", case, o)).value == 1.0
    assert (await score("recall_at_k", case, o, k=1)).value == 0.0
    assert (await score("precision_at_k", case, o)).value == pytest.approx(1 / 3, abs=1e-5)
    assert (await score("mrr", case, o)).value == 0.5
    assert (await score("ndcg_at_k", case, o)).value == pytest.approx(0.63093, abs=1e-4)  # 1/log2(3)
    assert (await score("hit_rate", case, o)).passed is True
    na = await score("recall_at_k", TestCase(id="n"), o)
    assert na.value is None and "expected_sources" in na.details["not_applicable"]


async def test_generation_safety_agent_and_ops_metrics():
    case = TestCase(
        id="q",
        reference="Employees get 25 days of annual leave.",
        must_include=["25 days", "annual leave"],
        must_not_include=["password"],
        expected_sources=["hb.md#annual"],
        expected_tools=["search_documents", "calculator"],
    )
    o = obs(
        answer="Employees receive 25 days of paid annual leave [1].",
        citations=[{"n": 1, "id": "c2", "filename": "hb.md", "section": "Handbook > Annual leave"}],
        invalid_citations=[3],
        tools_used=["search_documents"],
        latency_ms=1200.0,
        input_tokens=100,
        output_tokens=20,
        cost_usd=0.001,
        retries=1,
    )
    assert (await score("contains_required", case, o)).passed is True
    assert 0.7 < (await score("reference_f1", case, o)).value < 1
    assert (await score("citation_validity", case, o)).value == 0.5
    assert (await score("citation_source_recall", case, o)).value == 1.0
    assert (await score("forbidden_content", case, o)).value == 1.0
    tools = await score("tool_use", case, o)
    assert tools.value == 0.5 and tools.details["missing"] == ["calculator"]
    assert (await score("tokens", case, o)).value == 120 and (await score("retries", case, o)).value == 1
    failed = Observation(run_id="r", status="failed", error={"code": "X"})
    assert (await score("success", case, failed)).value == 0.0
    assert (await score("contains_required", case, failed)).value == 0.0  # no answer counts as missing
    assert token_f1("the cat sat", "a cat sat") == 1.0
    # v1.1: citing nothing is fine when the case expects no sources (e.g. "the handbook does not say")
    unanswerable = TestCase(id="u", reference="Not covered.")
    assert (await score("citation_validity", unanswerable, obs(answer="I don't know."))).value is None
    assert (await score("citation_validity", case, obs(answer="No citations here."))).value == 0.0


class ScriptedJudge:
    def __init__(self, score):
        self.score, self.requests = score, []

    async def chat(self, request, *, api_key=None, base_url=None):
        self.requests.append(request)
        call = ToolCall(id="1", name="submit_grade", arguments={"score": self.score, "reasoning": "because"})
        return ChatResponse(
            text="",
            tool_calls=[call],
            usage=ModelUsage(provider="x", model="j", input_tokens=9, estimated_cost_usd=0.0001),
        )


async def test_llm_judge_records_provenance(monkeypatch):
    prov = ScriptedJudge(4)
    monkeypatch.setattr(judge_mod, "get_provider", lambda name: prov)
    judge = judge_mod.Judge(JudgeConfig(provider="mock", model="judge-1"), None)
    case = TestCase(id="q", inputs={"question": "How much leave?"}, reference="25 days")
    m = await score("correctness_judge", case, obs(answer="25 days of leave"), judge=judge)
    assert m.value == 0.75 and m.passed is True
    assert m.details["judge"]["model"] == "judge-1" and m.details["judge"]["prompt"] == "correctness-v1"
    assert "How much leave?" in prov.requests[0].messages[0].content and prov.requests[0].tool_choice == "required"
    g = await score("groundedness_judge", case, obs(answer="x", retrieved=DOCS), judge=judge)
    assert g.details["judge"]["prompt"] == "groundedness-v1" and "[1] Travel" in prov.requests[1].messages[0].content
    assert judge.input_tokens == 18
    no_judge = await score("correctness_judge", case, obs(answer="x"))
    assert no_judge.value is None


def test_dataset_validation_and_hash():
    with pytest.raises(ValueError):
        DatasetContent.model_validate({"cases": [{"id": "a"}, {"id": "a"}]})
    a = DatasetContent.model_validate({"cases": [{"id": "a", "inputs": {"q": "x"}, "expected_sources": ["f.md#S"]}]})
    b = DatasetContent.model_validate(
        {"cases": [{"id": "a", "inputs": {"q": "x"}, "expected_sources": [{"filename": "f.md", "section": "S"}]}]}
    )
    assert a.content_hash() == b.content_hash()


HANDBOOK = b"""# Handbook

## Annual leave
Every employee receives 25 days of paid annual leave per year. Unused days expire on 31 March.

## Expenses
Hotels are reimbursed up to 180 EUR per night.

## Security
Report phishing emails to the security team within one hour.
"""


def rag_doc(file_id, retriever_type="agenticmind.rag.retriever"):
    def n(i, t, v="1.0.0", **c):
        return {"id": i, "type": t, "version": v, "config": c}

    def d(i, s, t):
        (a, b), (c, e) = s.split("."), t.split(".")
        return {"id": i, "kind": "data", "source": {"node": a, "port": b}, "target": {"node": c, "port": e}}

    return load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_eval", "name": "Eval"},
            "nodes": [
                n("emb", "agenticmind.rag.embedding_model", provider="hash", dimensions=256),
                n("store", "agenticmind.rag.vector_store", collection="docs"),
                n("load", "agenticmind.data.document_loader", files=[file_id]),
                n("chunk", "agenticmind.rag.chunker", strategy="markdown", chunk_size=40, chunk_overlap=0),
                n("index", "agenticmind.rag.index_documents", "1.1.0"),
                n("report", "agenticmind.core.output", key="report"),
                n("question", "agenticmind.core.manual_input", key="question"),
                n("retrieve", retriever_type, top_k=3),
                n("context", "agenticmind.rag.context_builder"),
                n("llm", "agenticmind.models.llm", "1.1.0", provider="mock", model="mock-1"),
                n("cite", "agenticmind.rag.citation_builder"),
                n("answer", "agenticmind.core.output", key="answer"),
            ],
            "edges": [
                d("e1", "load.documents", "chunk.documents"),
                d("e2", "chunk.chunks", "index.documents"),
                d("e3", "emb.model", "index.model"),
                d("e4", "store.store", "index.store"),
                d("e5", "index.report", "report.value"),
                d("q1", "question.value", "retrieve.query"),
                d("q2", "emb.model", "retrieve.model"),
                d("q3", "store.store", "retrieve.store"),
                d("q4", "retrieve.documents", "context.documents"),
                d("q5", "question.value", "context.question"),
                d("q6", "context.prompt", "llm.prompt"),
                d("q7", "llm.text", "cite.answer"),
                d("q8", "context.sources", "cite.sources"),
                d("q9", "cite.result", "answer.value"),
            ],
        }
    )


DATASET = DatasetContent.model_validate(
    {
        "cases": [
            {
                "id": "leave",
                "inputs": {"question": "annual leave days per year"},
                "expected_sources": ["handbook.md#annual leave"],
                "must_include": ["25 days"],
            },
            {
                "id": "hotel",
                "inputs": {"question": "hotel reimbursed per night EUR"},
                "expected_sources": ["handbook.md#expenses"],
                "must_include": ["180 EUR"],
            },
            {
                "id": "phishing",
                "inputs": {"question": "report phishing emails security team"},
                "expected_sources": ["handbook.md#security"],
            },
        ]
    }
)


async def test_run_evaluation_end_to_end(tmp_path):
    registry = default_registry()
    store = LocalArtifactStore(tmp_path / "artifacts")
    file_id = store.put_bytes("prj_eval", "handbook.md", HANDBOOK).id
    doc = rag_doc(file_id)
    kwargs = {"artifacts": store, "data_dir": tmp_path}
    await Engine(observer=InMemoryObserver(), **kwargs).run(compile_plan(doc, registry, targets=["report"]))

    seen = []

    class Hooks:
        def case_done(self, result):
            seen.append(result.case_id)

    config = EvaluationConfig(
        evaluators=["recall_at_k", "mrr", "contains_required", "citation_validity", "success", "latency_ms"], k=3
    )
    out = await run_evaluation(doc, registry, DATASET, config, engine_kwargs=kwargs, hooks=Hooks())
    assert sorted(seen) == ["hotel", "leave", "phishing"]
    assert out["setup"]["retrieval_node"] == "retrieve" and out["setup"]["answer_output"] == "answer"
    summary = {m["name"]: m for m in out["summary"]}
    assert summary["success"]["mean"] == 1.0 and summary["recall_at_k"]["n"] == 3
    assert summary["recall_at_k"]["mean"] > 0.6 and "p95" in summary["latency_ms"]
    assert summary["contains_required"]["n"] == 2 and summary["contains_required"]["of"] == 3  # 'phishing' has none
    leave = next(c for c in out["cases"] if c.case_id == "leave")
    assert leave.run_id.startswith("run_") and leave.observation.retrieved and leave.observation.answer

    # the same dataset against a second version of the graph, then compare
    worse = rag_doc(file_id)
    for node in worse.nodes:
        if node.id == "retrieve":
            node.config["top_k"] = 1
    out_b = await run_evaluation(worse, registry, DATASET, config, engine_kwargs=kwargs)
    diff = compare(
        {"summary": out["summary"], "cases": [c.model_dump() for c in out["cases"]]},
        {"summary": out_b["summary"], "cases": [c.model_dump() for c in out_b["cases"]]},
    )
    assert {m["name"] for m in diff["metrics"]} >= {"recall_at_k", "mrr"}
    assert diff["only_in_a"] == [] and len(diff["cases"]) == 3
    assert diff["regressions"] >= 0 and diff["note"]


async def test_configuration_errors(tmp_path):
    registry = default_registry()
    doc = rag_doc("art_0000000000000000")
    with pytest.raises(EvaluationError, match="Unknown evaluators"):
        await run_evaluation(doc, registry, DATASET, EvaluationConfig(evaluators=["nope"]))
    with pytest.raises(EvaluationError, match="No Output node or key"):
        await run_evaluation(doc, registry, DATASET, EvaluationConfig(evaluators=["success"], answer_output="missing"))
    with pytest.raises(EvaluationError, match="not part of"):
        await run_evaluation(doc, registry, DATASET, EvaluationConfig(evaluators=["success"], retrieval_node="report"))


async def test_judge_failure_is_not_applicable_not_zero(tmp_path, monkeypatch):
    class Broken:
        async def chat(self, request, *, api_key=None, base_url=None):
            return ChatResponse(text="", usage=ModelUsage(provider="x", model="j"), finish_reason="length")

    monkeypatch.setattr(judge_mod, "get_provider", lambda name: Broken())
    registry = default_registry()
    store = LocalArtifactStore(tmp_path / "artifacts")
    doc = rag_doc(store.put_bytes("prj_eval", "handbook.md", HANDBOOK).id)
    kwargs = {"artifacts": store, "data_dir": tmp_path}
    await Engine(observer=InMemoryObserver(), **kwargs).run(compile_plan(doc, registry, targets=["report"]))
    data = DatasetContent.model_validate(
        {"cases": [{"id": "a", "inputs": {"question": "leave"}, "reference": "25 days"}]}
    )
    config = EvaluationConfig(evaluators=["correctness_judge"], judge=JudgeConfig(provider="mock"))
    out = await run_evaluation(doc, registry, data, config, engine_kwargs=kwargs)
    m = out["cases"][0].metric("correctness_judge")
    assert m.value is None and "no grade" in m.details["error"]
    assert out["summary"][0]["n"] == 0 and out["setup"]["judge"]["model"]

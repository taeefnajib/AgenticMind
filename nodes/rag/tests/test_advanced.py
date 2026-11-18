"""Advanced retrieval: keyword index, hybrid fusion, reranking, query rewriting, compression."""

import importlib

import pytest
from agenticmind_ir import load_project
from agenticmind_node_sdk import ModelUsage
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_core import register as register_core
from agenticmind_nodes_data import register as register_data
from agenticmind_nodes_rag import register as register_rag
from agenticmind_providers import ChatResponse, ToolCall
from agenticmind_registry import NodeRegistry, TrustLevel
from agenticmind_runtime import Engine, InMemoryObserver, LocalArtifactStore, RunStatus, compile_plan

adv = importlib.import_module("agenticmind_nodes_rag.advanced")
MOCK_LLM = {"llm_provider": "mock", "llm_model": "mock-1"}

HANDBOOK = b"""# Handbook

## Annual leave
Every employee receives 25 days of paid annual leave per year. Unused days expire on 31 March.

## Expenses
Submit expense claims with receipts within 30 days. Hotels are reimbursed up to 180 EUR per night.

## Security
Report phishing emails to the security team within one hour. Never share your password.
Error code SEC-4471 means the account is locked after five failed logins.
"""


@pytest.fixture
def registry():
    reg = NodeRegistry()
    for fn in (register_core, register_data, register_rag):
        fn(lambda d: reg.register(d, trust=TrustLevel.CORE))
    return reg


def n(node_id, t, version="1.0.0", **config):
    return {"id": node_id, "type": t, "version": version, "config": config}


def d(eid, src, tgt):
    (sn, sp), (tn, tp) = src.split("."), tgt.split(".")
    return {"id": eid, "kind": "data", "source": {"node": sn, "port": sp}, "target": {"node": tn, "port": tp}}


def graph(file_id, query_nodes, query_edges, index_version="1.1.0"):
    return load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_adv", "name": "Adv"},
            "nodes": [
                n("emb", "agenticmind.rag.embedding_model", provider="hash", dimensions=256),
                n("store", "agenticmind.rag.vector_store", collection="docs"),
                n("load", "agenticmind.data.document_loader", files=[file_id]),
                n("chunk", "agenticmind.rag.chunker", strategy="markdown", chunk_size=40, chunk_overlap=0),
                n("index", "agenticmind.rag.index_documents", version=index_version),
                n("report", "agenticmind.core.output", key="report"),
                n("question", "agenticmind.core.manual_input", key="question"),
                *query_nodes,
            ],
            "edges": [
                d("e01", "load.documents", "chunk.documents"),
                d("e02", "chunk.chunks", "index.documents"),
                d("e03", "emb.model", "index.model"),
                d("e04", "store.store", "index.store"),
                d("e05", "index.report", "report.value"),
                *query_edges,
            ],
        }
    )


async def run(registry, tmp_path, document, inputs=None, targets=None):
    engine = Engine(
        observer=(obs := InMemoryObserver()), artifacts=LocalArtifactStore(tmp_path / "a"), data_dir=tmp_path
    )
    result = await engine.run(compile_plan(document, registry, targets=targets), inputs=inputs or {})
    return result, obs


@pytest.fixture
def file_id(tmp_path):
    return LocalArtifactStore(tmp_path / "a").put_bytes("prj_adv", "handbook.md", HANDBOOK).id


HYBRID = [
    n("hybrid", "agenticmind.rag.hybrid_search", top_k=3, candidates=10),
    n("kw", "agenticmind.rag.keyword_search", top_k=2),
    n("results", "agenticmind.core.output", key="results"),
    n("kw_results", "agenticmind.core.output", key="kw_results"),
]
HYBRID_EDGES = [
    d("q1", "question.value", "hybrid.query"),
    d("q2", "emb.model", "hybrid.model"),
    d("q3", "store.store", "hybrid.store"),
    d("q4", "hybrid.documents", "results.value"),
    d("q5", "question.value", "kw.query"),
    d("q6", "store.store", "kw.store"),
    d("q7", "kw.documents", "kw_results.value"),
]


async def test_index_builds_keyword_index_and_hybrid_finds_exact_terms(registry, tmp_path, file_id):
    document = graph(file_id, HYBRID, HYBRID_EDGES)
    result, _ = await run(registry, tmp_path, document, targets=["report"])
    report = result.outputs["report"]
    assert report["keyword_indexed"] == report["documents"] and report["keyword_index_size"] == report["documents"]
    # re-indexing skips unchanged chunks for both indexes
    result, _ = await run(registry, tmp_path, document, targets=["report"])
    assert result.outputs["report"]["keyword_indexed"] == 0 and result.outputs["report"]["embedded"] == 0

    result, obs = await run(
        registry, tmp_path, document, {"question": "What does SEC-4471 mean?"}, ["results", "kw_results"]
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    kw = result.outputs["kw_results"]
    assert kw[0]["source"]["section"].endswith("Security") and kw[0]["retrieval_method"] == "keyword"
    hybrid = result.outputs["results"]
    assert hybrid[0]["source"]["section"].endswith("Security") and hybrid[0]["retrieval_method"] == "hybrid"
    event = next(e.data for e in obs.events if e.data.get("type") == "retrieved" and e.data.get("method") == "hybrid")
    assert event["top"][0]["keyword_rank"] == 1 and event["keyword_hits"] >= 1


async def test_keyword_index_is_backfilled_for_collections_indexed_before(registry, tmp_path, file_id):
    old = graph(file_id, HYBRID, HYBRID_EDGES, index_version="1.0.0")
    result, _ = await run(registry, tmp_path, old, targets=["report"])
    assert "keyword_indexed" not in result.outputs["report"]
    result, _ = await run(registry, tmp_path, old, {"question": "SEC-4471"}, ["kw_results"])
    assert result.status == RunStatus.FAILED and result.error.code == "KEYWORD_INDEX_NOT_FOUND"
    # hybrid still works (vector only) and says so
    result, obs = await run(registry, tmp_path, old, {"question": "SEC-4471"}, ["results"])
    assert result.status == RunStatus.SUCCEEDED and any(
        e.data.get("type") == "keyword_index_missing" for e in obs.events
    )
    new = graph(file_id, HYBRID, HYBRID_EDGES)
    result, _ = await run(registry, tmp_path, new, targets=["report"])
    assert result.outputs["report"]["embedded"] == 0 and result.outputs["report"]["keyword_indexed"] > 0


def test_fusion_functions():
    fused = adv.rrf_fuse([["a", "b", "c"], ["c", "a"]], k=60)
    assert max(fused, key=fused.get) == "a" and fused["c"] > fused["b"]
    w = adv.weighted_fuse({"a": 0.9, "b": 0.1}, {"b": 12.0, "c": 3.0}, vector_weight=0.5)
    assert w["a"] == 0.5 and w["b"] == 0.5 and w["c"] == 0.0
    assert adv.weighted_fuse({"a": 1.0}, {}, 1.0) == {"a": 1.0}


DOCS = [
    {
        "id": "d1",
        "content": "Hotels are reimbursed up to 180 EUR per night.",
        "source": {"uri": "x"},
        "retrieval_method": "vector",
        "score": 0.9,
    },
    {
        "id": "d2",
        "content": "Annual leave is 25 days per year.",
        "source": {"uri": "x"},
        "retrieval_method": "vector",
        "score": 0.8,
    },
    {
        "id": "d3",
        "content": "Every employee receives paid annual leave. Unused days expire.",
        "source": {"uri": "x"},
        "retrieval_method": "vector",
        "score": 0.7,
    },
]


async def test_reranker_reorders_and_thresholds():
    r = await invoke_node(
        adv.reranker,
        inputs={"query": "how many annual leave days", "documents": DOCS},
        config={"provider": "overlap", "top_k": 2},
    )
    ids = [x["id"] for x in r.outputs["documents"]]
    assert ids[0] in ("d2", "d3") and "d1" not in ids and r.outputs["documents"][0]["rerank_score"] > 0
    assert r.outputs["documents"][0]["score"] in (0.8, 0.7)  # first-stage score is kept
    none = await invoke_node(
        adv.reranker,
        inputs={"query": "pizza", "documents": DOCS},
        config={"provider": "overlap", "score_threshold": 0.5},
    )
    assert none.outputs["documents"] == []


async def test_llm_reranker_and_rewriter(monkeypatch):
    class Scripted:
        def __init__(self, name, args):
            self.name, self.args = name, args

        async def chat(self, request, *, api_key=None, base_url=None):
            self.request = request
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(id="1", name=self.name, arguments=self.args)],
                usage=ModelUsage(provider="x", model="m", input_tokens=7),
            )

    prov = Scripted(
        "submit_scores", {"scores": [{"index": 0, "score": 2}, {"index": 2, "score": 9}, {"index": 9, "score": 10}]}
    )
    monkeypatch.setattr(adv, "get_provider", lambda name: prov)
    r = await invoke_node(
        adv.reranker, inputs={"query": "leave", "documents": DOCS}, config={"provider": "llm", **MOCK_LLM}
    )
    assert [x["id"] for x in r.outputs["documents"]] == ["d3", "d1", "d2"]
    assert [x["rerank_score"] for x in r.outputs["documents"]] == [0.9, 0.2, 0.0]

    prov = Scripted(
        "submit_queries",
        {
            "standalone": "annual leave days in the Handbook",
            "variants": ["vacation allowance", "paid time off", "annual leave days in the Handbook"],
        },
    )
    monkeypatch.setattr(adv, "get_provider", lambda name: prov)
    history = [
        {"role": "user", "content": "Tell me about the Handbook"},
        {"role": "assistant", "content": "It covers leave and expenses."},
    ]
    r = await invoke_node(
        adv.query_rewriter,
        inputs={"query": "how many days of it?", "history": history},
        config={**MOCK_LLM, "variants": 2},
    )
    assert r.outputs["query"] == "annual leave days in the Handbook"
    assert r.outputs["queries"] == ["annual leave days in the Handbook", "vacation allowance", "paid time off"]
    assert "Tell me about the Handbook" in prov.request.system

    prov = Scripted("submit_queries", {"standalone": ""})
    monkeypatch.setattr(adv, "get_provider", lambda name: prov)
    r = await invoke_node(adv.query_rewriter, inputs={"query": "original?"}, config={**MOCK_LLM, "variants": 0})
    assert r.outputs == {"query": "original?", "queries": ["original?"]}


async def test_extractive_compression_keeps_relevant_sentences():
    long = {
        "id": "h",
        "source": {"uri": "x", "section": "Leave"},
        "retrieval_method": "hybrid",
        "score": 1.0,
        "offsets": {"start": 0, "end": 300},
        "content": "The company was founded in 1990. Every employee receives 25 days of paid annual leave per year. "
        "Unused days expire on 31 March. The cafeteria serves lunch daily. Parking is free.",
    }
    off_topic = {
        "id": "o",
        "content": "The cafeteria serves lunch daily.",
        "source": {"uri": "x"},
        "retrieval_method": "hybrid",
        "score": 0.5,
    }
    r = await invoke_node(
        adv.context_compressor,
        inputs={"query": "annual leave days", "documents": [long, off_topic]},
        config={"max_sentences": 1, "neighbors": 1},
    )
    docs = r.outputs["documents"]
    assert [x["id"] for x in docs] == ["h"] and docs[0]["source"]["section"] == "Leave" and "offsets" not in docs[0]
    assert "25 days of paid annual leave" in docs[0]["content"] and "Parking" not in docs[0]["content"]
    assert r.outputs["report"]["documents_out"] == 1 and r.outputs["report"]["ratio"] < 0.7


async def test_llm_compression_never_invents_text(monkeypatch):
    class Picker:
        async def chat(self, request, *, api_key=None, base_url=None):
            picked = ["Unused days expire on 31 March.", "Employees also get a free car."]  # second is invented
            return ChatResponse(
                text="",
                tool_calls=[ToolCall(id="1", name="submit_sentences", arguments={"sentences": picked})],
                usage=ModelUsage(provider="x", model="m"),
            )

    monkeypatch.setattr(adv, "get_provider", lambda name: Picker())
    doc = {
        "id": "h",
        "content": "Leave is 25 days. Unused days expire on 31 March.",
        "source": {"uri": "x"},
        "retrieval_method": "v",
        "score": 1,
    }
    r = await invoke_node(
        adv.context_compressor, inputs={"query": "expiry", "documents": [doc]}, config={"method": "llm", **MOCK_LLM}
    )
    assert r.outputs["documents"][0]["content"] == "Unused days expire on 31 March."


def test_sentence_splitting():
    assert adv.split_sentences("One. Two? Three!\n- bullet item\n\nFour 1.5 EUR.") == [
        "One.",
        "Two?",
        "Three!",
        "- bullet item",
        "Four 1.5 EUR.",
    ]

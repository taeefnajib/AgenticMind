"""End-to-end retrieval through the real engine: hash embeddings + embedded Qdrant/Chroma."""

import pytest
from agenticmind_ir import load_project
from agenticmind_nodes_core import register as register_core
from agenticmind_nodes_data import register as register_data
from agenticmind_nodes_rag import register as register_rag
from agenticmind_registry import NodeRegistry, TrustLevel
from agenticmind_runtime import (
    Engine,
    InMemoryObserver,
    LocalArtifactStore,
    MappingSecretResolver,
    PlanError,
    RunStatus,
    compile_plan,
)

HANDBOOK = b"""# Handbook

## Annual leave
Every employee receives 25 days of paid annual leave per year. Unused days expire on 31 March.

## Expenses
Submit expense claims with receipts within 30 days. Hotels are reimbursed up to 180 EUR per night.

## Security
Report phishing emails to the security team within one hour. Never share your password.
"""


@pytest.fixture
def registry():
    reg = NodeRegistry()
    for fn, src in ((register_core, "core"), (register_data, "data"), (register_rag, "rag")):
        fn(lambda d, s=src: reg.register(d, trust=TrustLevel.CORE, source=s))
    return reg


def n(node_id, t, **config):
    return {"id": node_id, "type": t, "version": "1.0.0", "config": config}


def d(eid, src, tgt):
    (sn, sp), (tn, tp) = src.split("."), tgt.split(".")
    return {"id": eid, "kind": "data", "source": {"node": sn, "port": sp}, "target": {"node": tn, "port": tp}}


def rag_doc(file_id, backend="qdrant", emb=None):
    emb = emb or {"provider": "hash", "dimensions": 256}
    return load_project(
        {
            "ir_version": "1.0",
            "project": {"id": "prj_test", "name": "RAG"},
            "nodes": [
                n("emb", "agenticmind.rag.embedding_model", **emb),
                n("store", "agenticmind.rag.vector_store", backend=backend, collection="docs"),
                n("load", "agenticmind.data.document_loader", files=[file_id]),
                n("chunk", "agenticmind.rag.chunker", strategy="markdown", chunk_size=40, chunk_overlap=0),
                n("index", "agenticmind.rag.index_documents"),
                n("report", "agenticmind.core.output", key="report"),
                n("question", "agenticmind.core.manual_input", key="question"),
                n("retrieve", "agenticmind.rag.retriever", top_k=2),
                n("results", "agenticmind.core.output", key="results"),
            ],
            "edges": [
                d("e01", "load.documents", "chunk.documents"),
                d("e02", "chunk.chunks", "index.documents"),
                d("e03", "emb.model", "index.model"),
                d("e04", "store.store", "index.store"),
                d("e05", "index.report", "report.value"),
                d("e06", "question.value", "retrieve.query"),
                d("e07", "emb.model", "retrieve.model"),
                d("e08", "store.store", "retrieve.store"),
                d("e09", "retrieve.documents", "results.value"),
            ],
        }
    )


@pytest.fixture
def env(tmp_path):
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    info = artifacts.put_bytes("prj_test", "handbook.md", HANDBOOK)
    return artifacts, info.id, tmp_path


async def run(registry, doc, env, targets=None, inputs=None, secrets=None):
    artifacts, _, data_dir = env
    obs = InMemoryObserver()
    plan = compile_plan(doc, registry, targets=targets, secrets=secrets)
    engine = Engine(observer=obs, artifacts=artifacts, data_dir=data_dir, secrets=secrets)
    return await engine.run(plan, inputs=inputs or {}), obs, plan


@pytest.mark.parametrize("backend", ["qdrant", "chroma"])
async def test_ingest_then_query_with_targets(registry, env, backend):
    doc = rag_doc(env[1], backend=backend)

    # query before ingestion: clear, actionable error
    result, _, plan = await run(registry, doc, env, targets=["results"], inputs={"question": "leave"})
    assert set(plan.nodes) == {"emb", "store", "question", "retrieve", "results"}
    assert result.status == RunStatus.FAILED and result.error.code == "COLLECTION_NOT_FOUND"

    # ingestion only
    result, obs, plan = await run(registry, doc, env, targets=["report"])
    assert "question" not in plan.nodes and "retrieve" not in plan.nodes
    assert result.status == RunStatus.SUCCEEDED, result.error
    report = result.outputs["report"]
    assert report["embedded"] == report["documents"] == 4 and report["skipped_unchanged"] == 0
    assert report["collection"] == "prj_test__docs" and report["dimensions"] == 256
    assert obs.events[0].data["targets"] == ["report"]

    # re-ingest: everything unchanged is skipped
    result, _, _ = await run(registry, doc, env, targets=["report"])
    assert result.outputs["report"]["embedded"] == 0 and result.outputs["report"]["skipped_unchanged"] == 4

    # query only
    result, obs, _ = await run(
        registry, doc, env, targets=["results"], inputs={"question": "how many days of annual leave do I get"}
    )
    assert result.status == RunStatus.SUCCEEDED, result.error
    hits = result.outputs["results"]
    assert len(hits) == 2 and hits[0]["source"]["section"] == "Handbook > Annual leave"
    assert hits[0]["retrieval_method"] == "vector" and hits[0]["score"] >= hits[1]["score"]
    assert hits[0]["offsets"]["end"] > hits[0]["offsets"]["start"]


async def test_model_mismatch_is_detected(registry, env):
    await run(registry, rag_doc(env[1]), env, targets=["report"])  # indexed as "hash:hash@256"
    # same vector size (hash default is 256) but a different model identity
    other = rag_doc(env[1], emb={"provider": "hash"})
    result, _, _ = await run(registry, other, env, targets=["results"], inputs={"question": "leave"})
    assert result.status == RunStatus.FAILED and result.error.code == "EMBEDDING_MODEL_MISMATCH"
    # different vector size is caught when indexing
    bigger = rag_doc(env[1], emb={"provider": "hash", "dimensions": 128})
    result, _, _ = await run(registry, bigger, env, targets=["report"])
    assert result.status == RunStatus.FAILED and result.error.code == "EMBEDDING_DIMENSION_MISMATCH"


async def test_recreate_allows_switching_dimensions(registry, env):
    await run(registry, rag_doc(env[1]), env, targets=["report"])
    doc = rag_doc(env[1], emb={"provider": "hash", "dimensions": 128})
    doc.node("index").config["recreate"] = True
    result, _, _ = await run(registry, doc, env, targets=["report"])
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert result.outputs["report"]["dimensions"] == 128


async def test_filters_and_threshold(registry, env):
    doc = rag_doc(env[1])
    await run(registry, doc, env, targets=["report"])
    doc.node("retrieve").config.update({"filters": {"section": "Handbook > Security"}, "top_k": 5})
    result, _, _ = await run(registry, doc, env, targets=["results"], inputs={"question": "leave"})
    assert {h["source"]["section"] for h in result.outputs["results"]} == {"Handbook > Security"}
    doc.node("retrieve").config.update({"filters": {}, "score_threshold": 0.99})
    result, _, _ = await run(registry, doc, env, targets=["results"], inputs={"question": "zzz unrelated"})
    assert result.outputs["results"] == []


async def test_unknown_target(registry, env):
    with pytest.raises(PlanError) as exc:
        compile_plan(rag_doc(env[1]), registry, targets=["nope"])
    assert exc.value.report.errors[0].code.value == "RUN_TARGET_UNKNOWN"


def test_handles_delegate_secrets_to_consumers(registry, env):
    doc = rag_doc(
        env[1],
        emb={
            "provider": "openai_compatible",
            "model": "google/gemini-embedding-001",
            "api_key": {"$secret": "OPENROUTER_API_KEY"},
        },
    )
    plan = compile_plan(doc, registry)
    assert plan.nodes["index"].allowed_secrets == frozenset({"OPENROUTER_API_KEY"})
    assert plan.nodes["retrieve"].allowed_secrets == frozenset({"OPENROUTER_API_KEY"})
    assert plan.nodes["chunk"].allowed_secrets == frozenset()


async def test_api_embeddings_flow_uses_delegated_secret(registry, env, monkeypatch):
    """openai_compatible provider end to end with a mocked HTTP API; the key never reaches traces."""
    import httpx

    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        body = __import__("json").loads(request.content)
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1.0, float(i % 3)]} for i in range(len(body["input"]))]}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    doc = rag_doc(
        env[1],
        emb={
            "provider": "openai_compatible",
            "model": "google/gemini-embedding-001",
            "api_key": {"$secret": "OPENROUTER_API_KEY"},
        },
    )
    secrets = MappingSecretResolver({"OPENROUTER_API_KEY": "sk-or-test-secret"})
    result, obs, _ = await run(registry, doc, env, targets=["report"], secrets=secrets)
    assert result.status == RunStatus.SUCCEEDED, result.error
    assert seen and all(h == "Bearer sk-or-test-secret" for h in seen)
    assert "sk-or-test-secret" not in "".join(e.model_dump_json() for e in obs.events) + str(obs.outputs)

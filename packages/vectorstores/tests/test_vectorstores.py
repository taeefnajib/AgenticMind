"""Contract tests run against every backend.

Qdrant and Chroma run embedded (always). pgvector and Pinecone need external services and run only
when ``AGENTICMIND_TEST_PGVECTOR_DSN`` / ``AGENTICMIND_TEST_PINECONE_API_KEY`` are set.
"""

import math
import os
import uuid

import pytest
from agenticmind_vectorstores import (
    StoreRecord,
    StoreSpec,
    VectorStoreError,
    delete_local_collections,
    filter_fields,
    normalize_filter,
    open_store,
)


def unit(*xs):
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


def doc(i, filename="a.md", page=None, content=None):
    source = {"uri": f"artifact://x/{filename}", "filename": filename}
    if page:
        source["page"] = page
    return {"id": f"d{i}", "content": content or f"chunk {i}", "source": source, "metadata": {"lang": "en"}}


def record(i, vec, **kw):
    d = doc(i, **kw)
    return StoreRecord(
        id=d["id"], vector=vec, document=d, content_hash=f"h{i}", embedding_model="hash:hash@3", fields=filter_fields(d)
    )


BACKENDS = ["qdrant", "chroma"]
if os.environ.get("AGENTICMIND_TEST_PGVECTOR_DSN"):
    BACKENDS.append("pgvector")
if os.environ.get("AGENTICMIND_TEST_PINECONE_API_KEY"):
    BACKENDS.append("pinecone")


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    name = f"test-{uuid.uuid4().hex[:8]}"
    spec = StoreSpec(
        backend=request.param,
        collection=name,
        local_path=tmp_path / request.param,
        dsn=os.environ.get("AGENTICMIND_TEST_PGVECTOR_DSN"),
        api_key=os.environ.get("AGENTICMIND_TEST_PINECONE_API_KEY"),
    )
    s = open_store(spec)
    yield s
    s.drop()


def test_contract(store):
    assert not store.exists()
    store.ensure_collection(3)
    store.ensure_collection(3)  # idempotent
    assert store.exists() and store.count() == 0
    store.upsert(
        [
            record(1, unit(1, 0, 0)),
            record(2, unit(0, 1, 0), filename="b.pdf", page=2),
            record(3, unit(1, 1, 0)),
        ]
    )
    assert store.count() == 3
    hits = store.query(unit(1, 0.05, 0), top_k=2)
    assert [h.id for h in hits] == ["d1", "d3"]
    assert hits[0].score > hits[1].score and hits[0].score == pytest.approx(0.9988, abs=1e-3)
    assert hits[0].document["content"] == "chunk 1" and hits[0].embedding_model == "hash:hash@3"
    # filters
    assert [h.id for h in store.query(unit(1, 0, 0), top_k=5, filters={"filename": "b.pdf"})] == ["d2"]
    assert [h.id for h in store.query(unit(1, 0, 0), top_k=5, filters={"page": 2})] == ["d2"]
    assert len(store.query(unit(1, 0, 0), top_k=5, filters={"metadata.lang": "en"})) == 3
    # existing + overwrite
    assert store.existing(["d1", "d2", "nope"]) == {"d1": ("h1", "hash:hash@3"), "d2": ("h2", "hash:hash@3")}
    store.upsert([record(1, unit(0, 0, 1), content="changed")])
    assert store.count() == 3
    assert store.query(unit(0, 0, 1), top_k=1)[0].document["content"] == "changed"


def test_dimension_mismatch(store):
    store.ensure_collection(3)
    with pytest.raises(VectorStoreError) as exc:
        store.ensure_collection(4)
    assert exc.value.code == "EMBEDDING_DIMENSION_MISMATCH"


def test_drop_then_recreate_with_other_dimensions(store):
    store.ensure_collection(3)
    store.upsert([record(1, unit(1, 0, 0))])
    store.drop()
    assert not store.exists()
    store.ensure_collection(2)
    assert store.count() == 0
    store.upsert([record(1, unit(1, 0))])
    assert store.count() == 1


def test_filter_validation():
    assert normalize_filter({"filename": "a", "metadata.lang": "en"}) == {"filename": "a", "meta_lang": "en"}
    with pytest.raises(VectorStoreError):
        normalize_filter({"content": "x"})
    with pytest.raises(VectorStoreError):
        normalize_filter({"filename": ["a"]})


def test_unknown_backend():
    with pytest.raises(VectorStoreError):
        open_store(StoreSpec(backend="nope", collection="x"))


def test_local_cleanup_by_prefix(tmp_path):
    for backend in ("qdrant", "chroma"):
        for name in ("prj_a__docs", "prj_a__faq", "prj_b__docs"):
            s = open_store(StoreSpec(backend=backend, collection=name, local_path=tmp_path / "vectors" / backend))
            s.ensure_collection(3)
    removed = delete_local_collections(tmp_path, "prj_a__")
    assert sorted(removed) == ["chroma:prj_a__docs", "chroma:prj_a__faq", "qdrant:prj_a__docs", "qdrant:prj_a__faq"]
    remaining = open_store(
        StoreSpec(backend="qdrant", collection="prj_b__docs", local_path=tmp_path / "vectors" / "qdrant")
    )
    assert remaining.exists()

"""Backend-specific request details that the shared contract cannot see (ADR-0028)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agenticmind_vectorstores import BACKENDS, LOCAL_BACKENDS, StoreRecord, StoreSpec, filter_fields
from agenticmind_vectorstores.milvus_store import collection_name, filter_expr
from agenticmind_vectorstores.mongodb_store import search_pipeline
from agenticmind_vectorstores.redis_store import escape_tag, filter_query
from agenticmind_vectorstores.search_store import SearchEngineStore, index_name
from agenticmind_vectorstores.weaviate_store import class_name, object_uuid

sys.path.insert(0, str(Path(__file__).parent))
import fakes  # noqa: E402


def rec(i, vec):
    doc = {"id": f"d{i}", "content": f"c{i}", "source": {"uri": "u", "filename": "a.md"}, "metadata": {"lang": "en"}}
    return StoreRecord(id=doc["id"], vector=vec, document=doc, content_hash="h", embedding_model="m",
                       fields=filter_fields(doc))  # fmt: skip


def test_names_follow_each_engines_rules():
    assert index_name("Prj_A__Docs") == "prj_a__docs" and index_name("_x+y") == "x-y"
    assert class_name("prj_a__docs") == "Prj_a__docs" and class_name("9lives-x") == "C9lives_x"
    assert collection_name("prj-a.docs") == "prj_a_docs" and collection_name("1x") == "c_1x"
    assert object_uuid("d1") == object_uuid("d1") != object_uuid("d2")


def test_filter_syntaxes_escape_values():
    assert filter_expr({"filename": 'a "b".pdf', "page": 2, "meta_ok": True}) == (
        'filename == "a \\"b\\".pdf" and page == 2 and meta_ok == true'
    )
    assert escape_tag("q3-report.pdf") == "q3\\-report\\.pdf"
    assert filter_query({"filename": "a b.md", "page": 2, "meta_flag": False}, {"page"}) == (
        "@filename:{a\\ b\\.md} @page:[2 2] @meta_flag:{false}"
    )


def test_mongodb_prefilters_declared_fields_and_postfilters_meta():
    p = search_pipeline([1.0, 0.0], 3, {"filename": "a.md", "meta_lang": "en"})
    stage = p[0]["$vectorSearch"]
    assert stage["filter"] == {"filename": {"$eq": "a.md"}} and stage["limit"] == 60
    assert stage["numCandidates"] >= stage["limit"] * 20
    assert p[1] == {"$match": {"meta_lang": "en"}} and p[2] == {"$limit": 3}
    assert p[-1]["$project"]["score"] == {"$meta": "vectorSearchScore"}


def test_elasticsearch_requests():
    server = fakes.FakeSearchServer("elasticsearch")
    spec = StoreSpec(backend="elasticsearch", collection="Docs", url="https://elastic:pw@es.test:9243", api_key="k1")
    store = SearchEngineStore(spec, transport=server.transport())
    store.ensure_collection(2)
    store.upsert([rec(1, [1.0, 0.0])])
    store.query([1.0, 0.0], 1, {"filename": "a.md"})
    put = next(r for r in server.requests if r.method == "PUT")
    mapping = json.loads(put.content)["mappings"]
    assert put.url.path == "/docs" and put.headers["authorization"] == "ApiKey k1"
    assert mapping["properties"]["vector"]["index_options"] == {"type": "hnsw"}  # no quantisation
    assert mapping["properties"]["document"] == {"type": "object", "enabled": False}
    bulk = next(r for r in server.requests if r.url.path == "/_bulk")
    assert bulk.url.params["refresh"] == "wait_for"
    search = json.loads(next(r for r in server.requests if r.url.path.endswith("_search")).content)
    assert search["knn"]["filter"] == {"bool": {"filter": [{"term": {"filename": "a.md"}}]}}


def test_opensearch_requests():
    server = fakes.FakeSearchServer("opensearch")
    store = SearchEngineStore(StoreSpec(backend="opensearch", collection="docs", url="http://admin:pw@os.test:9200",
                                        distance="dot"), transport=server.transport())  # fmt: skip
    store.ensure_collection(2)
    body = json.loads(next(r for r in server.requests if r.method == "PUT").content)
    assert body["settings"] == {"index": {"knn": True}}
    assert body["mappings"]["properties"]["vector"]["method"] == {"name": "hnsw", "engine": "lucene",
                                                                   "space_type": "innerproduct"}  # fmt: skip
    store.upsert([rec(1, [2.0, 0.0])])
    hit = store.query([3.0, 0.0], 1, {"metadata.lang": "en"})[0]
    assert hit.score == 6.0  # exact dot product, not the engine's rescaled score
    search = json.loads(next(r for r in server.requests if r.url.path.endswith("_search")).content)
    assert search["query"]["knn"]["vector"]["filter"] == {"bool": {"filter": [{"term": {"meta_lang": "en"}}]}}
    assert "authorization" in {k.lower() for k in server.requests[0].headers}  # basic auth from the URL


def test_backend_catalogue():
    assert set(LOCAL_BACKENDS) == {"qdrant", "chroma", "faiss", "lancedb"}
    assert {"weaviate", "milvus", "elasticsearch", "opensearch", "mongodb", "redis", "faiss", "lancedb"} <= set(
        BACKENDS
    )

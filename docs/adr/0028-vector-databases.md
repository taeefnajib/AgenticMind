# ADR-0028: More vector databases (FAISS, LanceDB, Milvus, Weaviate, Elasticsearch, OpenSearch, MongoDB Atlas, Redis)

**Status:** Accepted. This is step 4 of the "build any agentic workflow" plan (steps 1–3 are ADR-0025 to
ADR-0027).

## Context
Users asked for "all the popular vector databases such as Weaviate, FAISS, etc.". AgenticMind had Qdrant
(the default, embedded), Chroma, pgvector and Pinecone behind one synchronous `VectorStore` contract
(ADR-0010), tested by a shared contract suite.

## Decision
Eight backends are added behind the same contract. They store the same thing: the full document JSON,
content hash and embedding-model id, plus flat filter fields (`filename`, `artifact_id`, `page`,
`section`, `parent_id`, `meta_<key>`). So Index Documents, Retriever, Hybrid Search, parent-child
chunking and evaluation work unchanged on every one.

| backend | where it runs | notes |
|---|---|---|
| faiss | embedded files under the data dir | exact `IndexFlatIP` in an `IndexIDMap2`, with a SQLite sidecar for documents and fields; filtered queries score the matching vectors exactly |
| lancedb | embedded, or LanceDB Cloud (`db://…` + api_key, region) | flat filter fields are columns pre-filtered in SQL; `meta_*` filtered exactly in Python over an enlarged candidate set; `merge_insert` upserts |
| milvus | server / Zilliz Cloud (URL + token), or Milvus Lite file locally (Linux/macOS) | string primary keys, dynamic fields, `consistency_level=Strong`; COSINE/IP results are similarities |
| weaviate | server or Weaviate Cloud (URL + api_key) | client v4, `Configure.Vectors.self_provided`; uuid5 object ids; dimensions recorded in the collection description |
| elasticsearch | URL + api_key (or user:password in the URL) | REST via httpx: `dense_vector` (cosine / `max_inner_product`, plain `hnsw` index options, so no quantisation), top-level `knn` with `filter` |
| opensearch | URL with user:password | REST: `index.knn`, `knn_vector` on the Lucene engine (cosinesimil / innerproduct), `knn` query with in-search `filter` |
| mongodb | Atlas, dsn secret (`namespace` = database) | `vectorSearch` index with declared filter paths, which pre-filter; `meta_*` via `$match` after the search; waits until the index is queryable |
| redis | Redis 8 / Redis Stack, URL or dsn secret | HNSW vector field plus TAG/NUMERIC fields; new `meta_*` fields are added with `FT.ALTER`, so every filter runs inside the KNN query |

- **One score scale:** scores mean "cosine similarity, or dot product" everywhere. Engines that rescale scores
  (Elasticsearch, OpenSearch, MongoDB, LanceDB, Weaviate) have their scores recomputed from the returned
  vector. Redis's distance is converted (1 − d). Milvus's similarity is used directly.
- **Clear errors:** connection, auth and missing-collection failures map to the existing `STORE_*` /
  `COLLECTION_NOT_FOUND` errors. Unreachable servers are retryable.
- **Vector Store node:** offers all 12 backends. URL, key, DSN, region and namespace appear per backend
  (`show_when`), and the config validates what each backend needs. FAISS, LanceDB, Qdrant, Chroma and Milvus
  Lite keep data under the server's data directory when no URL is set; project deletion removes embedded
  collections.
- **Exporter and deployment:** the exporter adds the right `agenticmind-vectorstores[...]` extra and
  describes the service. Embedded backends mark the app as needing persistent data, so deployment
  capability checks block targets without volumes (for example Cloud Run).
- **Docs check (Nov 2026):** Weaviate (v4.16+ vector config), Milvus `MilvusClient`, Elasticsearch knn,
  OpenSearch k-NN, MongoDB `$vectorSearch` and Redis vector search. Where a doc left a score formula open,
  the exact rescoring removes the dependency.

## Verification
- **Contract suite against the real thing:** FAISS and LanceDB, alongside Qdrant and Chroma; Milvus as
  well wherever Milvus Lite is installed (Linux CI).
- **Contract suite against in-memory fakes** of the APIs the adapters call: Milvus, Weaviate,
  Elasticsearch, OpenSearch, MongoDB Atlas and Redis (`tests/fakes.py`). The fakes check request shapes,
  for example only declared filter paths in `$vectorSearch`, only indexed fields in Redis queries, and the
  ndjson bulk format.
- **Request-detail tests:** the Elasticsearch mapping has no quantisation and sends an `ApiKey` header (an
  explicit key wins over URL credentials, a bug found by this test); the OpenSearch Lucene method and filter
  placement; filter escaping for Milvus and Redis; MongoDB pre/post-filter split; name rules.
- **End to end through the engine:** index and query with FAISS and LanceDB (hash embeddings).
- **Deployment:** FAISS needs persistent data and is blocked on Cloud Run; Weaviate is an external service.
- **Opt-in live contract runs:** `AGENTICMIND_TEST_WEAVIATE_URL`, `_ELASTICSEARCH_URL`,
  `_OPENSEARCH_URL`, `_MONGODB_URI` and `_REDIS_URL`, alongside the existing pgvector and Pinecone variables.

## Consequences
- None of Milvus, Weaviate, Elasticsearch, OpenSearch, MongoDB Atlas or Redis was run against a real server
  in this step (no Docker on the build machine). The fakes prove the adapters' logic, not the vendors'
  behaviour.
- The API image grows: faiss-cpu, lancedb + pyarrow, pymilvus + grpcio/pandas, weaviate-client, pymongo and
  redis.
- FAISS rewrites its index file on every upsert (fine for project-sized collections) and is single-process.
- LanceDB and MongoDB filter `meta_*` keys after the search over 20–50× candidates, which is exact for
  collections up to that size. Amazon OpenSearch SigV4 and Elasticsearch quantisation are not offered.

## Revisit when
Live CI services (testcontainers), hybrid search inside an engine (Elasticsearch/OpenSearch BM25, Weaviate
hybrid), quantisation options, or more stores (Azure AI Search, Vespa, Turbopuffer) are needed.

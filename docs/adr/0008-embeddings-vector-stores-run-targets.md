# ADR-0008: Embeddings, vector stores, handles and run targets

**Context.** RAG needs embeddings and a vector database, with an offline, open-source default and
optional hosted services (user decision: local open-source by default; provider APIs optional; Qdrant,
Chroma, pgvector and Pinecone all supported). Ingestion and querying must be separate flows.

**Decision.**
- **Embedding providers** behind `EmbeddingProvider` (`packages/providers/embeddings.py`):
  `fastembed` (default, `BAAI/bge-small-en-v1.5`, local ONNX, downloaded once into
  `<data_dir>/models`), `openai_compatible` (any `POST /embeddings` API; default base URL OpenRouter,
  default model `google/gemini-embedding-001`), and `hash` (deterministic, tests/offline only).
- **Vector stores** behind `VectorStore` (`packages/vectorstores`): Qdrant (default; embedded local mode
  at `<data_dir>/vectors/qdrant`, or server/cloud via URL), Chroma (embedded or server), pgvector (dsn
  secret) and Pinecone (api_key secret). Optional backends are extras and imported lazily. Every vector
  stores the full document, a content hash and the embedding-model identity; scores are always
  "higher = more similar" (only cosine and dot are offered).
- **Handles.** *Embedding Model* and *Vector Store* nodes output small descriptors
  (`EmbeddingModel`, `VectorStoreHandle` port types) consumed by *Index Documents* and *Retriever*, so
  ingestion and query always share the model and collection. Handles carry secret *references*; the
  plan delegates them to consumers wired to the handle that declare `credentials`.
- **Safety nets.** Re-indexing skips unchanged chunks (content hash + model identity); dimension
  mismatches fail at index time (`EMBEDDING_DIMENSION_MISMATCH`) and model mismatches at query time
  (`EMBEDDING_MODEL_MISMATCH`); collections are prefixed with the project ID by default and embedded
  collections are deleted with their project.
- **Run targets.** `POST /runs {targets: [...]}` executes only the targets and their upstream
  dependencies (data and control edges). The IDE offers "Run → <output>" per Output node.

**Consequences.** Works offline out of the box (after a one-time ~65 MB model download). pgvector and
Pinecone adapters are written against the installed client APIs (psycopg 3.3, pinecone 10.3) and covered
by the same contract tests, which run only when `AGENTICMIND_TEST_PGVECTOR_DSN` /
`AGENTICMIND_TEST_PINECONE_API_KEY` are set; they have not been run against live services yet.
Qdrant's embedded mode can resurrect points when a collection is deleted and recreated in the same
process (observed on Windows), so the adapter empties a collection before dropping it.

**Revisit when.** Adding hybrid search (Step 4) or when collections need explicit lifecycle management in the UI.

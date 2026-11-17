# Roadmap

Status of blueprint milestones. Update this file whenever behaviour changes.

| Milestone | Status |
|---|---|
| M0 Foundation | **Done**: monorepo, local dev, IR v1 + JSON Schema, node contract, persistence |
| M1 Visual architecture | **Done (core)**: canvas, library, typed ports, inspector, live validation |
| M2 First-party runtime | **Done (core)**: plan, registry, handlers, traces, retries, timeouts, cancellation |
| M3 RAG vertical slice | **In progress**: steps 1 (ingestion) and 2 (embeddings, vector stores, retrieval) done; step 3 (LLM answers with citations) next |
| M4 Compiler / API export | Planned |
| M5 Evaluation Lab | Planned |
| M6 Agent Lab | Planned |
| M7 Semantic intelligence (NVIDIA Auto Ontology adapter) | Planned: inspect upstream first |
| M8 Deployment Center | Planned (Docker self-hosting exists for the platform itself) |
| M9 Ecosystem | Partly: entry-point discovery and trust levels exist |

## Implemented

- **IR v1**: Pydantic models, published schema with drift test, canonical JSON and content hash, chained
  migrations, secret references.
- **Node SDK**: port types and compatibility, manifests (identity, config schema, ports, branches,
  permissions, execution policy, compatibility, modes), `NodeContext` with scoped secrets and custom
  events, `invoke_node` test helper.
- **Registry**: entry-point discovery, semver ordering, trust assignment by distribution.
- **Runtime**: 7-stage validation with 25 stable codes; deterministic plans; concurrent scheduler;
  control routing with skip propagation; explicit joins; error-class-aware retries; timeouts;
  cancellation; output contract checks; redacted traces with token usage; CLI.
- **Core nodes**: Manual Input, Output, Delay, Text Template, Text Transform, JSON Select, To Text,
  Parse JSON, Collect, Conditional, LLM (mock provider).
- **API**: projects with append-only revisions and optimistic concurrency, validation of unsaved
  documents, runs with IR snapshots, persisted events and node outputs, SSE with resume, cancel, canonical
  IR export/import, starter templates, orphaned-run detection.
- **Web IDE**: node library (search, drag/click to add), React Flow canvas with typed coloured ports and
  control handles, client-side connection checks mirroring the validator, inspector with schema-driven
  config forms (enum/number/bool/text/JSON/secret), live diagnostics on nodes, run inputs form, live trace,
  output and token/cost totals, per-node outputs, no-code / low-code / developer modes, Ctrl+S, export,
  import, new-from-template.
- **Step 1 – ingestion**: `Document`/`RetrievedDocument` schemas with runtime port checks; project file
  storage (`LocalArtifactStore`, upload/list/download/delete API, type allowlist, size limit, `artifacts`
  permission); Document Loader (PDF per page, DOCX headings → Markdown, TXT/MD/HTML/CSV/JSON); Document
  Cleaner; Chunker (recursive / fixed / markdown, tokens or characters, exact offsets, section paths);
  file picker + Files panel + document viewer in the IDE; *Document ingestion* template;
  `examples/data/employee-handbook.md`; GitHub Actions CI.
- **Step 2 – embeddings & retrieval**: embedding providers (fastembed local default, OpenAI-compatible
  APIs with OpenRouter/Gemini default, hash for tests); vector store adapters (Qdrant embedded default,
  Chroma, pgvector, Pinecone) with a shared contract test suite; Embedding Model, Vector Store, Index
  Documents (skips unchanged chunks) and Retriever nodes; handles with secret delegation; run targets
  (API + "Run → output" selector); node storage dirs; project deletion removes embedded collections;
  *RAG retrieval* template; similarity scores in the document viewer.
- **Ops**: Dockerfile (API + built IDE), compose with optional Postgres profile.

## Known limitations

- Runs execute in the API process (ADR-0005); no durable checkpoints, resume or human-approval suspension.
- No process/container isolation: non-core nodes are refused (ADR-0004). Declared permissions other
  than `credentials` are not yet enforced at OS level.
- Only the deterministic mock model provider exists.
- No authentication, workspaces or roles yet; the API must not be exposed publicly.
- Schema is created with `create_all`; Alembic migrations needed before production databases.
- No subgraphs, loops, explicit parallel/join nodes, or streaming execution mode.
- pgvector and Pinecone adapters share the contract tests but have not yet been run against live
  services (set `AGENTICMIND_TEST_PGVECTOR_DSN` / `AGENTICMIND_TEST_PINECONE_API_KEY`).
- Pure vector search with the small default model can rank a related-but-wrong section first
  (e.g. "vacation" → Travel just above Annual leave); reranking and hybrid search arrive in step 4.
- OCR for scanned PDFs is not supported yet (loader reports `NO_TEXT_EXTRACTED`). The token measure used
  for chunk sizes is an approximation, not a model tokenizer.
- Version history is available via the API (`/revisions`) but has no UI yet.
- Licence not chosen.
- The Docker image build is defined but was not exercised in CI.

## Next tasks (in order)

1. **Step 3 – RAG with citations**: OpenRouter chat provider (Gemini default, verify current API docs
   first); Context Builder (token budget, dedup, source labels); Citation Builder; interactive chat test
   panel targeting the answer output.
2. **Step 4 – advanced retrieval**: BM25 keyword search, hybrid fusion, reranker, query rewriting.
3. Compiler (M4): IR → standalone FastAPI project embedding the runtime, OpenAPI, Dockerfile, tests,
   build manifest; REST Trigger/Response nodes.
4. Evaluation Lab (M5): versioned datasets, evaluators (retrieval metrics first), runs comparing two
   revisions with per-case results.
5. Durable run backend: checkpoint abstraction, queue-backed workers, resume endpoint, Human Approval.
6. AuthN/Z, workspaces, audit log; Alembic migrations.
7. Revision history UI and diff view (canonical IR makes this straightforward).

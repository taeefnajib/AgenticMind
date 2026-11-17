# Roadmap

Status of blueprint milestones. Update this file whenever behaviour changes.

| Milestone | Status |
|---|---|
| M0 Foundation | **Done**: monorepo, local dev, IR v1 + JSON Schema, node contract, persistence |
| M1 Visual architecture | **Done (core)**: canvas, library, typed ports, inspector, live validation |
| M2 First-party runtime | **Done (core)**: plan, registry, handlers, traces, retries, timeouts, cancellation |
| M3 RAG vertical slice | **Core done**: ingestion, retrieval, cited answers with a real LLM; step 4 (hybrid search, reranking, query rewriting) pending |
| M4 Compiler / API export | Planned |
| M5 Evaluation Lab | Planned |
| M6 Agent Lab | **In progress**: 8a (tools + bounded Agent) done; 8b–8e next |
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
- **Step 3 – grounded answers**: OpenRouter / OpenAI-compatible chat provider (Gemini default
  `google/gemini-3.8-flash`, tool-calling types, cost tracking, classified errors); LLM node 1.1.0 (1.0.0
  kept for existing projects; library shows latest versions only); Context Builder (numbered sources,
  dedup, token budget, editable prompt); Citation Builder (marker parsing, invalid-citation removal,
  optional enforcement); Chat panel with clickable citations, latency, tokens and cost; *RAG answers with
  citations* template; verified live with Gemini (correct cited answer; "I don't know" when unsupported).
- **8a – tools & Agent**: `Tool` handles with transitive secret delegation; Agent node (bounded loop: turns,
  tool calls, tokens, cost; schema-validated arguments; per-tool timeouts; errors returned to the model;
  `agent_turn`/`tool_call` trace events); tools: Knowledge Search, Calculator (safe AST), Date & Time,
  Web Fetch (domain allowlist, private-address and redirect checks); mock provider tool calling for
  offline tests; chat shows tool steps and renders Markdown safely; *Agent with tools* template;
  verified live with Gemini (parallel searches + calculator; date + search + calculator).
- **Ops**: Dockerfile (API + built IDE), compose with optional Postgres profile.

## Known limitations

- Runs execute in the API process (ADR-0005); no durable checkpoints, resume or human-approval suspension.
- No process/container isolation: non-core nodes are refused (ADR-0004). Declared permissions other
  than `credentials` are not yet enforced at OS level.
- Agent tools run in-process (core trust only); third-party tool isolation is future work. Web Fetch's
  DNS check does not defend against DNS rebinding between check and connect.
- Chat is single-turn (no conversation memory yet). Citation validation checks that cited sources exist,
  not that they support the claim (faithfulness evaluation comes with the Evaluation Lab).
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

1. **Section 8 – Agents** (continuing): 8b Loop / Router / Parallel-Join; 8c memory (short-term state,
   consented long-term memory); 8d durable checkpoints, Human Approval, resume; 8e planner/worker,
   supervisor, reflection/critic, handoff; third-party tools via an entry-point group.
2. **Step 4 – advanced retrieval**: BM25 keyword search, hybrid fusion, reranker, query rewriting,
   context compression.
3. Compiler (M4): IR → standalone FastAPI project embedding the runtime, OpenAPI, Dockerfile, tests,
   build manifest; REST Trigger/Response nodes.
4. Evaluation Lab (M5): versioned datasets, evaluators (retrieval metrics first), runs comparing two
   revisions with per-case results.
5. Durable run backend: checkpoint abstraction, queue-backed workers, resume endpoint, Human Approval.
6. AuthN/Z, workspaces, audit log; Alembic migrations.
7. Revision history UI and diff view (canonical IR makes this straightforward).

# Roadmap

Status of blueprint milestones. Update this file whenever behaviour changes.

| Milestone | Status |
|---|---|
| M0 Foundation | **Done**: monorepo, local dev, IR v1 + JSON Schema, node contract, persistence |
| M1 Visual architecture | **Done (core)**: canvas, library, typed ports, inspector, live validation |
| M2 First-party runtime | **Done (core)**: plan, registry, handlers, traces, retries, timeouts, cancellation |
| M3 RAG vertical slice | **Done**: ingestion, hybrid retrieval (vector + BM25), reranking, query rewriting, compression, cited answers |
| M4 Compiler / API export | **Done (core)**: standalone FastAPI export with OpenAPI, tests, Docker assets, vendored runtime; Docker build not yet exercised |
| M5 Evaluation Lab | **Done (core)**: versioned datasets, 18 evaluators (retrieval, generation, safety, agent, operations, LLM judges), traced per-case runs, comparison and baseline |
| M6 Agent Lab | **Done (core)**: tools + Agent, loops/router/join, memory, checkpoints/approval/resume, multi-agent patterns (8a–8e) |
| M7 Semantic intelligence (NVIDIA Auto Ontology adapter) | **In progress**: M7a database nodes done (connector, schema inspector, profiler, read-only SQL policy, NL-to-SQL, SQL tool); M7b Auto Ontology adapter next (upstream inspected: Apache-2.0, no releases, pin commit `778ccde`) |
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
- **8b – loops, router, join**: Loop / Loop End (paired acyclic regions; `for_each` with concurrency and
  skip-on-error, `repeat` with state and a stop condition; nested loops; per-iteration scopes and
  `data.iteration` on events); loop validation (`LOOP_UNPAIRED`, `LOOP_SCOPE_SHARED`, `LOOP_REGION_LEAK`,
  `LOOP_OVERLAP`); Router (routes from config become branches; rules or LLM via a forced tool call);
  Join (waits for all control paths; collect/merge/concat); IDE shows dynamic route handles and
  "ran ×N"; *Ticket triage* template; verified live with Gemini routing.
- **8c – memory**: conversation state (Conversation History, Save Conversation Turn; Agent 1.1.0 `history`
  input) kept separate from long-term memories (Remember, Recall, Memory Tool recall/save); explicit
  consent wired in the graph (chat toggle, off by default); retention per entry; project + namespace
  isolation; keyword recall with recency fill; API and Memory tab to list and delete; multi-turn chat
  with "New chat"; *Assistant with memory* template; verified live with Gemini.
- **8d – checkpoints, approval, resume**: exact per-node checkpoints; `can_suspend` nodes pause runs as
  `waiting` (no worker held); Human Approval node (approve / reject with comment / edit); approval inbox
  API and Approvals tab; `POST /runs/{id}/resume` with schema-validated responses and atomic claim; interrupted
  runs resume from checkpoints (side-effect guard with `force`); cancel waiting runs; *Draft and approve*
  template; verified live with Gemini, including an API restart while the run waited.
- **8e – multi-agent patterns**: Subagent tool (supervisor handoff, depth cap, usage roll-up, labelled
  traces), Planner, Critic (rule checks separate from model judgments), Loop End 1.1.0 `next_state`,
  `agenticmind.tools` entry points for third-party tool implementations; *Research team*, *Plan and
  execute* and *Write with a critic* templates; verified live with Gemini (critic loop revised a draft
  that failed 'accurate' and passed on round 2; planner + 3 parallel workers + synthesis; supervisor
  delegating to an analyst). Live testing added: subagents return partial work at limits, a wrap-up turn
  after turn/tool-call limits, clearer errors when reasoning models run out of output tokens, and a
  configurable Web Fetch User-Agent (`AGENTICMIND_HTTP_USER_AGENT`) for sites that require contact details.
- **Step 4 – advanced retrieval**: BM25 keyword index maintained by Index Documents 1.1.0 (SQLite FTS5,
  backfilled for older collections); Keyword Search; Hybrid Search (RRF or weighted fusion, multi-query);
  Reranker (local cross-encoder by default, LLM or overlap); Query Rewriter (standalone + variants, falls
  back to the original); Context Compressor (extractive, or LLM limited to source sentences); *Advanced
  RAG* template; existing RAG templates build keyword indexes; verified live with Gemini.
- **M4 – compiler / app export**: `agenticmind-compiler` generates a standalone FastAPI project from a saved
  revision: endpoints derived from Output/Manual Input contracts (customisable via `metadata.api`), Pydantic
  models, API-key auth, rate limit, OpenAPI 3.1, JSON schemas, examples, contract tests, Dockerfile, compose,
  `.env.example` (names only), README and a build manifest; vendored AgenticMind packages with pinned
  third-party versions; bundled uploads; ruff-formatted and linted output; deterministic ZIP. *Export app*
  dialog in the IDE. Verified: all templates compile and pass their own tests; the Advanced RAG export was
  installed with `uv sync` from the ZIP alone and served real cited answers via Gemini.
- **M5 – Evaluation Lab**: `agenticmind-evaluation` package (test cases, versioned evaluators, LLM judge
  with stored model/prompt/rubric, runner, comparison); every case runs as a normal traced run; immutable
  dataset versions; summaries disclose n/of and aggregation; comparison flags dataset/K/judge mismatches
  and per-case regressions; baseline per dataset; Evaluation Lab UI (datasets editor + JSON import/export,
  run form, results, compare); sample handbook dataset. Verified live with Gemini on two graph revisions.
- **M7a – semantic data nodes**: `agenticmind-nodes-semantic` (Database port/handle; Database Connector,
  Schema Inspector, Table Profiler, SQL Validator/Executor, NL-to-SQL with retries and an explicit
  "not answerable" path, SQL Tool for agents via the tool entry point); read-only enforced by a sqlglot
  policy *and* read-only connections; SQLite uploads (content-addressed copies) and PostgreSQL (dsn secret);
  chat shows the SQL and rows; sample `examples/data/sales.sqlite`; *Ask your database* template.
  Verified live with Gemini against independently computed answers.
- **Node library hover cards**: description, ports, branches and permissions on hover or focus.
- **Ops**: Dockerfile (API + built IDE), compose with optional Postgres profile.

## Known limitations

- Runs execute in the API process (ADR-0005). Waiting runs are durable and interrupted runs can be resumed
  from checkpoints (ADR-0013), but there is no queue-backed worker, approval expiry or assignee yet; loop
  iterations are not checkpointed and the Agent cannot pause before individual tool calls.
- No process/container isolation: non-core nodes are refused (ADR-0004). Declared permissions other
  than `credentials` are not yet enforced at OS level.
- Agent tools run in-process (core trust only); third-party tool isolation is future work. Web Fetch's
  DNS check does not defend against DNS rebinding between check and connect.
- Memory recall is keyword-based (plus recency), not semantic; memory isolation is per project and
  namespace only (no user identity yet). Text is not scanned for secrets before it is stored. Citation validation checks that cited sources exist,
  not that they support the claim (faithfulness evaluation comes with the Evaluation Lab).
- No authentication, workspaces or roles yet; the API must not be exposed publicly.
- Schema is created with `create_all`; Alembic migrations needed before production databases.
- No subgraphs or streaming execution mode. Loop iterations are not checkpointed; a body node's final
  status shows its last iteration (the IDE adds "ran ×N").
- pgvector and Pinecone adapters share the contract tests but have not yet been run against live
  services (set `AGENTICMIND_TEST_PGVECTOR_DSN` / `AGENTICMIND_TEST_PINECONE_API_KEY`).
- The keyword index is a local SQLite file even for remote vector backends; deleting source documents does
  not remove their chunks from either index. Retrieval quality has not yet been measured systematically
  (that comes with the Evaluation Lab).
- OCR for scanned PDFs is not supported yet (loader reports `NO_TEXT_EXTRACTED`). The token measure used
  for chunk sizes is an approximation, not a model tokenizer.
- Version history is available via the API (`/revisions`) but has no UI yet.
- Chat does not render LaTeX math that models sometimes produce (`$...$` is shown as text).
- Evaluation: no significance testing or CI gating yet; datasets are written by hand (no generation from
  documents); agent scoring covers tool use only (not handoffs or approvals).
- Semantic: SQLite and PostgreSQL only (PostgreSQL not yet run against a live server); the SQL policy is a
  guard, not a firewall (use a read-only database role); no PII masking or column-level permissions.
- Licence not chosen.
- The Docker image build is defined but was not exercised in CI (neither for the platform nor for exported apps).
- Exported apps: human-approval pauses answer 409, memory is in process, rate limits are per process; endpoints
  are customised by editing `metadata.api` in the IR (no UI yet).

## Next tasks (in order)

1. **M7b – NVIDIA Auto Ontology adapter**: connection/health/capability checks against a deployed
   instance's public API (pinned commit `778ccde`, OpenAPI 1.3.0), term search/details, answerability,
   ask-question, semantic context for NL-to-SQL, agent tool; contract tests against the pinned spec.
2. Durable run backend: queue-backed workers on top of the checkpoint/resume contract (ADR-0013).
3. AuthN/Z, workspaces, audit log; Alembic migrations.
4. Revision history UI and diff view (canonical IR makes this straightforward).

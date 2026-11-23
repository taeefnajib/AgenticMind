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
| M7 Semantic intelligence (NVIDIA Auto Ontology adapter) | **Done**: M7a database nodes (connector, schema inspector, profiler, read-only SQL policy, NL-to-SQL, SQL tool); M7b Auto Ontology adapter (pinned commit `778ccde`, OpenAPI 1.3.0 contract tests, capability checks, verified live) |
| M8 Deployment Center | **Done (first slice)**: capability checks for 7 targets, dev/prod/enterprise profiles, Compose/Kubernetes/Cloud Run artifacts, hash-bound confirmation, local Docker deploy with health, switch-over and rollback; AWS/Azure/Vercel plan-only |
| M9 Ecosystem | **Done (first slice)**: `agenticmind-node` CLI (new/validate/test/pack), node packages installed from wheels into their own folder and run in isolated worker processes, approval of exact permissions (NODE_NOT_PERMITTED until approved), trust levels, import/export of wheels, Node packages UI |

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
  permission); Document Loader (PDF per page, DOCX headings → Markdown, XLSX sheets, PPTX slides + notes, EPUB chapters,
  TXT/MD/RST/HTML/CSV/TSV/JSON/JSONL/XML/YAML; ADR-0024); Document
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
- **M9 – Ecosystem**: third-party node packages end to end. The `agenticmind-node` CLI scaffolds,
  validates, tests and packs a package (lint-clean scaffold with tests); the platform installs the wheel into its own
  folder without importing it, reads manifests in an isolated worker, and runs every invocation in a fresh
  worker process (scrubbed environment, own directory, only the node's own secrets, storage only with
  `filesystem`, network guard, timeout). Packages need an approval of exactly their requested permissions;
  new versions need re-approval; disable/enable/remove/download. Non-core packages in the host environment
  and their agent tools are no longer imported in-process by default. Example: `examples/plugins/acme-text-nodes`.
- **M8 – Deployment Center**: `agenticmind-deployment` derives requirements (persistent data, in-process
  state, longest request, local models, secrets) and checks them against Local Docker, Docker server,
  Kubernetes, Cloud Run, AWS ECS, Azure Container Apps and Vercel (cited vendor limits); profiles;
  generated Compose/Kustomize/Cloud Run artifacts with secrets by name only; plans with permissions and steps,
  confirmed against their hash; opt-in local Docker deployment (127.0.0.1, health-checked switch-over,
  rollback, stop, logs); guided targets can be marked live with a URL; Deploy dialog with history.
- **M7b – NVIDIA Auto Ontology adapter**: `Ontology` port/handle (token as a secret reference); connection
  node with health, token and semantic-layer readiness checks; Term Search, Term Details, Ontology Context
  (grounds NL-to-SQL), Ask Auto Ontology (streamed, SQL + rows in Chat), Auto Ontology Tool for agents;
  pinned commit with a vendored OpenAPI subset and contract tests; *Ask Auto Ontology* template.
- **AI workflow designer** (ADR-0022): sparkle launcher (brand gradient) on the canvas opens a chat over a blurred backdrop;
  natural-language requests become whole-graph proposals validated with the editor's pipeline (up to 3
  repair rounds), applied as a new revision with Undo; works on blank and existing projects.
- **Sign-in and settings** (ADR-0023): first-run admin setup, username/password sign-in (scrypt, rate
  limited), HttpOnly session cookies stored as hashes, admin/member roles, People management, profile and
  password change, sign out other browsers, cross-site write guard; Settings page to choose the AI
  assistant's provider and model (browse the provider's models, test the connection), and the theme.
- **Agents and reasoning** (ADR-0029): Agent architectures (function calling with parallel tool calls, ReAct,
  plan-and-execute); Reasoning node with chain of thought / self-consistency, task decomposition, tree of thoughts,
  graph of thoughts, Monte Carlo tree search and backtracking (budgeted, explored tree traced); Tool Router; Tool
  Chain; *Think it through* template.
- **Triggers** (ADR-0035): Webhook (token / HMAC, ack or wait), Schedule (cron + time zone / interval), Queue
  (Redis Streams, SQS) and Chat (Slack, Telegram, WhatsApp, email) trigger nodes; a trigger service activating
  the latest saved revision, verifying each platform's signature, deduplicating retries, starting recorded runs
  and replying; Trigger panel in the IDE; *Chat bot* template.
- **Sandboxed code execution** (ADR-0034): `agenticmind-sandbox` (Docker containers by default: no network,
  read-only, limits, unprivileged; opt-in local process mode with Node's permission model); Code node (Python /
  JavaScript, code editor) and Code Interpreter Tool for agents; *Analyse data with code* template.
- **MCP client and server** (ADR-0033): MCP Server tool node (stdio / Streamable HTTP, secret env and
  bearer token, include/exclude, prefixes) whose tools an Agent uses; MCP Call node; projects exposed as MCP
  tools at `/api/mcp` (Manual Inputs become the schema, each call a recorded run, optional bearer token, host
  checks); `agenticmind mcp-serve` over stdio; Agent 1.2.0 / Tool Router 1.1.0 / Tool Chain 1.1.0 may run
  stdio MCP tools; *Agent with MCP tools* template.
- **Guardrails** (ADR-0031): Input Guard (length, PII redact/flag/block, prompt-injection heuristics incl.
  hidden Unicode and base64, banned words/patterns, LLM injection judge and allowed/denied topics; passed/blocked
  branches), Output Guard (JSON / JSON Schema, required/blocked patterns, PII and credential leaks, LLM policy
  rules, model repair loop with history; passed/failed branches), PII Redactor; *Guarded assistant* template.
- **More AI methods** (ADR-0030): Reasoning methods self-refine, Reflexion, multi-agent debate (optionally across
  models), mixture of agents, step-back prompting and program of thought (safe arithmetic evaluator); Classifier
  node (multi-label, confidence threshold, label branches); Summarizer node (stuff / map-reduce / refine);
  *Classify and summarise feedback* template.
- **Vector databases** (ADR-0028): FAISS and LanceDB (embedded), Milvus, Weaviate, Elasticsearch, OpenSearch,
  MongoDB Atlas Vector Search and Redis, next to Qdrant, Chroma, pgvector and Pinecone; one score scale; per-backend
  fields in the Vector Store node; exporter/deployment aware of embedded vs server stores.
- **Chunking and parsing** (ADR-0027): Chunker 1.1.0 with recursive, fixed, sentence, markdown, layout-aware,
  semantic (embeddings or lexical), document and parent-child strategies; Parent Expander; layout-aware PDF parsing
  (font-size headings) and structured HTML/DOCX; *RAG with parent-child chunks* template.
- **Data sources** (ADR-0026): Database Connector for PostgreSQL (Supabase, Neon, RDS...), MySQL/MariaDB, SQL
  Server/Azure SQL, Snowflake, Databricks, DuckDB/MotherDuck, ClickHouse and BigQuery with structured, secret-backed
  credentials and per-engine read-only limits; S3 Loader (AWS and S3-compatible); Settings → Secrets (encrypted,
  write-only) with set/unset status in config forms.
- **Vision and local models** (ADR-0025): `Image` port type; Image Input (upload in Run inputs or attach in
  Chat) and Image Loader; LLM 1.2.0 sends images to vision models; Ollama and Hugging Face providers on every
  model-using node; open-source OCR (RapidOCR) as an OCR node and inside the Document Loader for images and
  scanned PDFs; *Describe an image* template.
- **Model Router** (ADR-0024): one node picks a model per request by keywords, a small classifier model, or
  both (hybrid), with a default route, a forced-route input, a `model_route` trace event and summed usage;
  route editor in the inspector.
- **Projects in Settings** (ADR-0024): searchable list with Open and Delete; deleting needs `DELETE` typed
  in the UI and `?confirm=DELETE` plus an admin at the API.
- **Node library hover cards**: description, ports, branches and permissions on hover or focus.
- **Ops**: Dockerfile (API + built IDE), compose with optional Postgres profile.

## Known limitations

- Triggers run inside the API process: several API processes on one database would double-fire schedules and
  queue polls; missed schedules are not replayed; exported apps do not host triggers; chat platforms need a
  public HTTPS URL.

- Code execution: a fresh sandbox per call (no stateful sessions, no file inputs/outputs yet); standard library
  only unless the operator configures an image; process mode does not block network and is not isolation.

- MCP: a connection per tool call (stdio servers start each call; pooling next); no MCP resources, prompts,
  sampling, elicitation or OAuth yet; stdio MCP servers run unsandboxed with the user's privileges.

- Guardrails: PII detection is pattern-based (emails, phones, cards with Luhn, US SSNs, IBANs with checksum, IPv4,
  common credential formats); names and addresses need a model (not built). Injection heuristics are a first
  filter; the LLM judge is the stronger check. No toxicity classifier model; use Output Guard policies.

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
- Sign-in is local accounts in one shared workspace: no SSO/OIDC, MFA, password-reset email, API tokens,
  per-project permissions or audit log yet; the login rate limiter is per process. Serve over HTTPS
  (`AGENTICMIND_COOKIE_SECURE=1`) before exposing the API beyond localhost.
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
- Auto Ontology: pinned to a commit (no upstream releases); `check_answerable` is MCP-only upstream and not
  used; questions are stateless (no `conversation_id`).
- Deployment: no direct cloud provisioning yet (guided files or plan only); AWS/Azure/Vercel artifacts not
  generated; no built-in image scanning; Kubernetes/Cloud Run manifests validated structurally (Kustomize
  build), not against a live cluster; local deploy needs AGENTICMIND_LOCAL_DEPLOY=1 (any signed-in member may deploy).
- Node packages: one process per invocation (~0.3–0.6 s); no OS-level network/filesystem enforcement on
  Windows workers (container workers next); no package signing (`verified` reserved); package nodes cannot
  use project files or memory, provide agent tools, or be exported in standalone apps yet.
- Reasoning: MCTS uses model value estimates (no rollouts) and backtracking relies on model step checks; search
  methods cost more model calls (bounded by max_llm_calls). Program of thought evaluates arithmetic only (no
  general code execution); debate/mixture across models need one provider.
- Vector stores: Milvus, Weaviate, Elasticsearch, OpenSearch, MongoDB Atlas and Redis are verified against API fakes
  (and opt-in live runs), not real servers in CI; no OpenSearch SigV4; FAISS is single-process.
- Chunking: token sizes are approximate; PDF layout detection is heuristic (no table extraction, multi-column order
  follows the content stream).
- Databases: only SQLite and DuckDB are exercised live in CI; other engines are tested against mocked APIs/drivers.
  SQL Server, Snowflake, Databricks and BigQuery are read-only by policy (use read-only logins). No Oracle yet.
- Vision/OCR: no image resizing; OCR models cover Chinese and English; exported apps cannot take image uploads yet.
- Model Router: one provider for all routes; no per-route providers, fallback chains or cost budgets.
  Spreadsheet dates are read as serial numbers; legacy .xls/.ppt/.doc and scanned images (OCR) unsupported.
- AI designer: replies carry the whole graph (fine for small/medium graphs); validity is structural, not
  semantic; chat threads are per browser session only.
- Licence not chosen.
- The Docker image build is defined but was not exercised in CI (neither for the platform nor for exported apps).
- Exported apps: human-approval pauses answer 409, memory is in process, rate limits are per process; endpoints
  are customised by editing `metadata.api` in the IR (no UI yet).

## Next tasks (in order)

Plan "build any agentic workflow" (one reviewed step each; all six steps done, ADR-0025 to ADR-0031, plus extra
AI methods in ADR-0030):
- Step 2: data sources: S3 / S3-compatible loader; database connector credentials for PostgreSQL/Supabase,
  MySQL, SQL Server, Databricks, Snowflake and other popular databases.
- Step 3: chunking and parsing: document, semantic, recursive character, parent-child, layout-aware.
- Step 4: vector databases: Weaviate, FAISS, Milvus, Elasticsearch/OpenSearch, MongoDB Atlas, Redis, LanceDB.
- Step 5: agents and reasoning: ReAct, chain/tree/graph of thoughts, task decomposition, MCTS, backtracking;
  function calling, tool routing, tool chaining.
- Step 6: guardrails: input and output validation (schema, PII, prompt injection, policy), retry and repair.

Open-source production plan (ADR-0032; one reviewed feature each):
- Done: MCP client and server (ADR-0033); sandboxed code execution (ADR-0034); triggers
  (ADR-0035).
- Next: native model providers; observability export; GitHub integration (push projects with version
  control); durable execution; streaming.

1. Version history panel and visual diff of revisions (requested in the blueprint, not yet built).
2. Durable run backend: queue-backed workers on top of the checkpoint/resume contract (ADR-0013).
3. Workspaces and per-project permissions, OIDC/SSO, API tokens, audit log; Alembic migrations.

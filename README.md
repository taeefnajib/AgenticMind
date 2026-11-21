<p align="center"><img src="docs/assets/logo-mark.svg" alt="AgenticMind logo" width="140"></p>

# AgenticMind

**Open-source AI engineering platform.** Build visually, simulate execution, test rigorously, evaluate
systematically, export real code, deploy anywhere.

The visual graph is the editable architecture source of truth. It is stored as a **canonical, versioned
IR** and executed by a **first-party runtime**. Frameworks (LangGraph etc.) will plug in as adapters, never
as the platform's data model. Users keep their architecture and source code.

> Status: **foundation slice** (milestones M0–M2 of the blueprint). You can create a project, add typed
> nodes on a canvas, connect them, get live validation with stable error codes, run the graph in the
> first-party runtime, watch node-level traces stream in, save/reload revisions and export the IR.
> See [docs/roadmap.md](docs/roadmap.md) for what is next (RAG slice, compiler/export, evaluation…).

## Quick start (local development)

Prerequisites: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
uv sync                 # Python workspace (all packages, editable)
npm install             # web IDE

npm run dev:api         # FastAPI on http://127.0.0.1:8000  (terminal 1)
npm run dev:web         # Vite on  http://localhost:5173     (terminal 2, proxies /api)
```

Open http://localhost:5173. On first launch a *Hello pipeline* project is created. Edit the *Run inputs*
tab and press **Run**.

To try document ingestion: **New** → *Document ingestion*, select the **Load documents** node, upload
`examples/data/employee-handbook.md` (or any PDF, Word, Excel, PowerPoint, EPUB, Markdown, HTML, CSV/TSV, JSON/JSONL, XML or YAML file), then **Run** and open the
**Output** tab to browse the chunks with their sections and offsets.

To try retrieval: **New** → *RAG retrieval*, upload a file in **Load documents**, pick **Run → Index report**
in the toolbar and run (the first run downloads the local embedding model once, ~65 MB). Then pick
**Run → Results**, type a question in **Run inputs** and run again to see the most relevant chunks with
similarity scores.

To get answers with citations: put your key in `.env` (`OPENROUTER_API_KEY=...`, see below) and restart the
API. **New** → *RAG answers with citations*, upload a file in **Load documents**, run **Run → Index report**,
then open the **Chat** tab and ask. Answers come from Gemini via OpenRouter and cite their sources; click a
citation number to see the source.

To try an agent: **New** → *Agent with tools*, upload a file, run **Run → Index report**, then ask in **Chat**,
e.g. *"How many leave days do 3 employees get in total?"*. The agent decides when to search the documents,
calculate or check the date; expand *Used N tool calls* to see every step.

To try loops and routing: **New** → *Ticket triage*, then **Run**. A Loop processes each ticket, a Router
sends it down the billing / technical / default route and Loop End collects the replies (nodes show
"ran ×N"). Set the Router's mode to *llm* to let Gemini classify instead of the regex rules.

To try memory: **New** → *Assistant with memory*, then chat in **Chat**. It remembers the conversation;
tick *Remember facts I share* to let it save facts for future chats (**New chat** starts fresh). Review
and delete everything in the **Memory** tab.

To try human-in-the-loop: **New** → *Draft and approve*, then **Run**. Gemini writes a draft and the run
pauses (nothing keeps running, and it survives a server restart). Open **Approvals**, edit the text if you
like and approve, or reject with a reason; the run continues from its checkpoint.

For better retrieval: **New** → *Advanced RAG* chains Query Rewriter → Hybrid Search (vectors + BM25
keywords) → Reranker (local cross-encoder) → Context Compressor before answering with citations.

To ship a graph as a service: **Export app** generates a standalone FastAPI project (endpoints from your
inputs/outputs, OpenAPI, tests, Dockerfile, the AgenticMind runtime vendored in). See
[docs/architecture/compiler.md](docs/architecture/compiler.md).

To measure quality: **Evaluate** opens the Evaluation Lab. Import `examples/datasets/employee-handbook-qa.json`,
run it against the current graph, change the graph, run again and compare (recall, MRR, required facts,
citations, LLM-judge correctness, latency, cost). See [docs/architecture/evaluation.md](docs/architecture/evaluation.md).

To query a database in plain language: **New** → *Ask your database*, upload `examples/data/sales.sqlite`
and ask in **Chat**; every answer shows its SQL, which runs read-only. See
[docs/architecture/semantic.md](docs/architecture/semantic.md).

To design with AI: click the ✨ **Build with AI** button in the bottom-right corner and describe what you want (e.g. "take
any question and answer yes or no"). The assistant builds or changes the graph in the selected project,
validates it, saves it as a new revision and offers Undo. It uses `OPENROUTER_API_KEY`; choose another
model under avatar → **Settings** → *AI assistant*. See ADR-0022.

To work with images: **New** → *Describe an image (vision)*, upload a picture in **Run inputs** (or attach one
in **Chat**) and ask about it. The LLM node (1.2.0) sends images to vision models; the **OCR** node and the
Document Loader read text from images and scanned PDFs with open-source RapidOCR, locally. Models can come
from OpenRouter, **Hugging Face** (`HF_TOKEN`), **Ollama** on your machine (no key) or any OpenAI-compatible
server. See ADR-0025.

To connect your data: the **Database Connector** speaks PostgreSQL (incl. Supabase, Neon), MySQL/MariaDB, SQL
Server, Snowflake, Databricks, DuckDB, ClickHouse and BigQuery; the **S3 Loader** reads documents from S3 or
S3-compatible storage. Add passwords and tokens under avatar → **Settings** → *Secrets* (encrypted, never shown
again) and refer to them by name in node settings. See ADR-0026.

To use different models for different tasks: add a **Model Router** node. It sends each request to a route
(e.g. code → a coding model, maths → a reasoning model, quick questions → a cheap model) chosen by
keywords, a small classifier model, or both. See ADR-0024.

To manage projects: avatar → **My projects** lists every saved project; deleting one asks you to type
`DELETE` first (admins only).

To add third-party nodes: build a package with `agenticmind-node new|validate|test|pack` (see
`examples/plugins/acme-text-nodes`), then Node library → **Node packages…** → install the wheel and approve
its permissions. Package nodes run in isolated worker processes. See
[docs/architecture/node-sdk.md](docs/architecture/node-sdk.md) and ADR-0021.

To deploy a project: **Deploy** checks it against Local Docker, a Docker server, Kubernetes, Cloud Run, AWS,
Azure and Vercel, shows the plan, and after you confirm deploys locally (opt-in) or generates the files;
history keeps health and rollback. See [docs/architecture/deployment.md](docs/architecture/deployment.md).

With an NVIDIA Auto Ontology deployment, **New** → *Ask Auto Ontology* asks questions through its semantic
layer (business terms, measures, relationships); see
[docs/architecture/auto-ontology.md](docs/architecture/auto-ontology.md).

Multi-agent templates: *Research team* (supervisor delegating to subagents), *Plan and execute*
(planner + parallel workers + synthesizer) and *Write with a critic* (revise until a critic passes).

### Accounts and sign-in

The first time you open the IDE it asks you to create the admin account. Admins add more people under
avatar → **Settings** → *People* (members build and run workflows; admins also manage people, workspace
settings and node packages). Profile, password and theme are in the same dialog. For a throwaway
single-user setup, `AGENTICMIND_AUTH=off` turns sign-in off. See ADR-0023.

### API keys and settings (`.env`)

The API loads a `.env` file from the directory it starts in (the repo root for `npm run dev:api`). Copy
`.env.example` to `.env` (git-ignored) and fill in what you need, e.g. `OPENROUTER_API_KEY=...`. Restart the
API after editing. In node configs, refer to a key by name (`$secret` → `OPENROUTER_API_KEY`); the value is
resolved at run time and never stored in projects, traces or exports.

Agents' **Web Fetch** tool identifies itself as `AgenticMind-WebFetch/0.1`. Many sites (Wikipedia among
them) only answer automated clients whose User-Agent includes contact details, so set your own, e.g.
`AGENTICMIND_HTTP_USER_AGENT=MyAgents/1.0 (https://example.org; you@example.org)`.

### Self-hosted (Docker)

```bash
docker compose up --build     # http://localhost:8000 — API + built IDE in one container, SQLite volume
```

### CLI (no UI, no server)

```bash
uv run agenticmind nodes
uv run agenticmind validate apps/api/src/agenticmind_api/templates/hello-pipeline.json
uv run agenticmind run apps/api/src/agenticmind_api/templates/hello-pipeline.json -i name=Ada --events
uv run agenticmind schema > ir.schema.json
```

## Development commands

| Command | What it does |
|---|---|
| `npm test` | Python tests + web typecheck + web unit tests |
| `uv run pytest -q` | Python tests (IR, SDK, registry, runtime, nodes, API) |
| `npm run typecheck:web` / `npm run test:web` | Strict TS check / Vitest |
| `npm run build:web` | Production build of the IDE |
| `uv run ruff check .` | Lint Python |
| `uv run python scripts/generate_schemas.py` | Regenerate the published IR JSON Schema after model changes |
| `uv run python scripts/build_logo.py` | Regenerate the logo, favicons and app icons from the vector definition |

## Repository layout

```
apps/
  api/            FastAPI control plane (projects, revisions, validation, runs, SSE traces, export)
  web/            React + TypeScript + React Flow visual IDE
packages/
  ir/             canonical IR: Pydantic models, JSON Schema, deterministic serialization, migrations
  node-sdk/       node contract: typed ports, manifests, config, handler/context, test helpers
  node-registry/  discovery via entry points, versions, trust levels
  runtime/        first-party engine: validation pipeline, execution planning, scheduler, traces, CLI
  providers/      model + embedding provider adapters (mock LLM; fastembed, OpenAI-compatible, hash)
  vectorstores/   vector store adapters (Qdrant default, Chroma, pgvector, Pinecone)
nodes/
  core/           core node package (inputs, transforms, conversions, control flow, LLM, outputs)
  data/           data ingestion nodes (Document Loader, Document Cleaner)
  rag/            RAG nodes (Chunker, Embedding Model, Vector Store, Index Documents, Retriever,
                  Context Builder, Citation Builder)
  agents/         Agent node and tools (Knowledge Search, Calculator, Date & Time, Web Fetch)
examples/data/    sample documents for trying ingestion and retrieval
docs/             architecture, roadmap, ADRs
infra/docker/     container build
```

## Documentation

- [Architecture overview](docs/architecture/overview.md): subsystem boundaries and dependency direction
- [IR v1](docs/architecture/ir-v1.md): graph schema and examples
- [Runtime](docs/architecture/runtime.md): execution semantics, lifecycle, events, error codes
- [Node SDK](docs/architecture/node-sdk.md): authoring and testing nodes
- [Roadmap](docs/roadmap.md): milestone status, known limitations, next tasks
- [ADRs](docs/adr/): architecture decisions

## Licence

Not yet chosen. The blueprint calls for an OSI-approved licence after dependency and ecosystem review
(tracked in the roadmap).

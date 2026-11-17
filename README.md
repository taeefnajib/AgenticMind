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
`examples/data/employee-handbook.md` (or any PDF/DOCX/TXT/MD/HTML/CSV), then **Run** and open the
**Output** tab to browse the chunks with their sections and offsets.

To try retrieval: **New** → *RAG retrieval*, upload a file in **Load documents**, pick **Run → Index report**
in the toolbar and run (the first run downloads the local embedding model once, ~65 MB). Then pick
**Run → Results**, type a question in **Run inputs** and run again to see the most relevant chunks with
similarity scores.

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
  rag/            RAG nodes (Chunker, Embedding Model, Vector Store, Index Documents, Retriever)
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

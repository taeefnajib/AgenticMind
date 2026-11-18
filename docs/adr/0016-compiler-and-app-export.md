# ADR-0016: Compiler and app export

**Status:** Accepted (M4)

## Context
The blueprint requires exporting a project as a readable, modular, runnable repository that does not
depend on hosted AgenticMind services; keeps the canonical IR for re-import; pins dependencies and records
node versions; never embeds credentials; includes tests, OpenAPI and Docker assets; and declares what it
needs. Request/response schemas come from the graph's trigger and output contracts.

## Decision
- **New package `agenticmind-compiler`** (IR → project files). It generates FastAPI source as text and
  never imports FastAPI, so the core dependency rules still hold. `apps/api` exposes
  `GET /api/projects/{id}/app/preview` and `GET /api/projects/{id}/app` (ZIP), and the IDE has an
  *Export app* dialog.
- **The API comes from the graph.** Each output key becomes `POST /<key>` (Output nodes sharing a key on
  alternative branches share one endpoint), plus `POST /run` when there are several. Each endpoint's request
  fields are the Manual Inputs among its run-target ancestors; response fields are typed from the port feeding
  each Output. `metadata.api.endpoints` (name, path, outputs, auth, rate limit, summary) overrides the
  defaults and is validated (`API_ENDPOINT_INVALID`, `API_UNKNOWN_OUTPUT`). Every endpoint's plan is compiled
  at export time and again at app startup, so an invalid graph fails early.
- **Standalone.** The first-party runtime and exactly the node packages the graph uses (plus their
  first-party dependencies) are vendored under `vendor/` with a `pyproject.toml` synthesised from installed
  metadata, so this works from editable and wheel installs. The app's `pyproject.toml` references them as uv
  path sources; `requirements.txt` serves pip and Docker. Third-party requirements, and only the extras the
  graph needs (e.g. `providers[local]` for fastembed, a vector-store backend), are pinned to the versions
  installed alongside the compiler. Uploaded files referenced by the graph are bundled under `data/`.
- **Security defaults.** API-key auth (`X-API-Key` against `APP_API_KEYS`, constant-time compare; protected
  endpoints answer 503 until keys are configured), a per-process rate limit, `extra="forbid"` request
  models, and secrets read from the environment by name. `.env.example` lists names only, and tests assert
  that no secret value appears in any exported file.
- **Pipeline step 6:** the compiler runs ruff (now a compiler dependency) with the export's own config
  to format, fix imports and lint the generated `app/` and `tests/`, and records the result in the manifest.
  Templates are written close to ruff style; ruff guarantees it.
- **Reproducible.** No timestamps in generated files; the ZIP has sorted entries with fixed dates and modes,
  so the same IR gives byte-identical output. `agenticmind.yaml` records the IR content hash, the compiler
  version and checks, node types/versions/packages, vendored packages, endpoints and requirements.
- **Generated tests:** health, OpenAPI contract against the exported `openapi.json`, unknown-field rejection,
  auth required, and a real run for endpoints that need no keys, network, model downloads or missing files.
- **Declared limits:** human-approval pauses return 409 (durable approvals need the AgenticMind server),
  memory is in process, the rate limit is per process, and nodes that read files with none selected are
  reported.

## Verification
All 14 templates compile; each export is ruff-clean and passes its own tests against its vendored packages.
The *Advanced RAG* export was installed into a fresh environment with `uv sync` (only the ZIP's contents)
and served: auth (401), validation (422), `/index-report` indexed the bundled handbook, and `/answer`
returned cited answers via Gemini. The Docker image build has not been exercised yet.

## Consequences
- Exports are larger (vendored source, ~0.4 MB) but have no dependency on unpublished packages.
- Changing generated output changes exported bytes; the compiler version in the manifest tracks this.

## Revisit when
AgenticMind packages are published to a package index (vendoring becomes optional), REST Trigger/Response
nodes or webhooks are added, or deployment targets (M8) consume the manifest's requirements.

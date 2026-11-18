# ADR-0019: NVIDIA Auto Ontology adapter (M7b)

**Status:** Accepted

## Context
Blueprint §9 names NVIDIA Auto Ontology as the semantic-intelligence integration and requires verifying
upstream before integrating; the M7 exit criterion is "pinned upstream integration passes contract tests
and capability checks". Inspection at the time of adoption (2026-11-19) found:
- Apache-2.0, **no releases**; the latest commit was `778ccdef75536bee5653306f8ded655924e4ada8`.
- A deployed **service stack**, not a library: a Next.js gateway (:3000) with Better Auth, a FastAPI backend
  (:3001) and an ingestion/compilation service (:3002), Postgres + pgvector, and NVIDIA NIM or
  OpenAI-compatible model endpoints. An optional MCP server (:3003) wraps the same API.
- Public gateway API: OpenAPI 3.1, version 1.3.0; API tokens are sent as `x-api-key` and act as their
  owner. Questions use `POST /api/chat/completions`, which streams SSE (`step`, `sql`, `result`,
  `charts`, `error`, then `[DONE]`).
- The internal FastAPI port trusts an `x-auto-ontology-user-id` header without verifying it.

## Decision
- **Adapter, not re-implementation.** Nodes in `agenticmind-nodes-semantic` call the gateway API through a
  small client (`auto_ontology.py`). New `Ontology` port type and `OntologyHandle` (base URL, token
  **secret reference**, timeout, optional target database). The handle travels between nodes like
  other handles; the token value never does.
- **Pinned and contract-tested.** `UPSTREAM_COMMIT` and `API_VERSION` are constants. The used operations
  and their schemas are vendored as `contract/auto-ontology-api.subset.json` by
  `scripts/extract_auto_ontology_contract.py`, with Apache-2.0 attribution. Tests run every client call
  through a fake gateway that rejects any method, path, query parameter or request body not declared in
  the pinned spec, and that validates its own fixture responses against the spec's response schemas.
  Row parsing mirrors upstream's MCP client at the same commit. An opt-in live test re-validates a real
  deployment's responses.
- **Capability-aware.** The connection node checks the health probe (it also detects a URL that isn't the
  gateway), checks the token by calling an authenticated endpoint, and checks whether the semantic layer
  is compiled. It reports `capabilities` and, by default, fails with `ONTOLOGY_NOT_READY` rather than
  letting questions fail later with a 409.
- **Nodes:**
  - Auto Ontology (connection).
  - Term Search and Term Details.
  - Ontology Context: deterministic term matching on names and synonyms; turns a question into term
    definitions, tables and SQL measures, e.g. to ground AgenticMind's own NL-to-SQL.
  - Ask Auto Ontology: streamed; step events go into the trace; the result has NL-to-SQL's shape, so Chat
    shows the SQL and rows.
  - Auto Ontology Tool for agents (`search_terms` / `ask_question`, through the tool entry point).
- **Gateway only.** Docs and errors point to `APP_URL` (:3000). The adapter never talks to :3001.
- **Not adopted:** the MCP server (the REST API covers the same operations without another service), and
  `check_answerable`, which is MCP-only at this commit. Conversations (`conversation_id`) are not used:
  each question is stateless, matching how AgenticMind runs other questions.

## Verification
Against a local deployment built from the pinned commit, serving a read-only copy of
`examples/data/sales.sqlite` (semantic layer: 4 terms, 3 relationships):
- The opt-in live contract test passes; the readiness check, term search, term details and context work.
  No token, a wrong token and the wrong port each fail with a clear error code. The token never appears
  in traces.
- Questions, each matched against an independent SQL query:
  - Top category by 2025 revenue excluding cancelled orders: Office, 88,792.70.
  - Delivered orders from Germany: 27.
  - Top 3 products by units in 2025, asked in the browser via the template: 264 / 251 / 247.
  - Average order value of delivered 2025 orders, through a Gemini agent using the Auto Ontology Tool:
    493.46.
- Live testing found two adapter bugs, both now fixed and covered by regression tests:
  - The engine's default 30-second node timeout cut off questions, so Ask now declares a 30-minute
    execution policy.
  - httpx timeouts bound each read, and a run keeps streaming step events, so a stuck upstream run never
    timed out. Questions now have a total deadline.
- Upstream's default NVIDIA-hosted embedding model (`nemotron-3.5-embed-1b`) answered "Service is
  overloaded" during testing, so its runs hung on retries. The local deployment was switched to
  `nvidia/nemotron-3-embed-1b` and recompiled. This is deployment config, not part of the adapter.

## Consequences
- Running it needs Docker, about 3 GB of images and NVIDIA model access. Without a deployment, the
  connection node fails with a clear `ONTOLOGY_UNREACHABLE`; nothing else in the platform depends on it.
- Upgrading the pin means re-extracting the contract; any breaking upstream change then fails the contract
  tests instead of failing at runtime.
- Upstream's own security posture applies: tokens act with their owner's role (use a least-privileged
  user), and without Vault the connection credentials added in its UI are stored unencrypted.

## Revisit when
Upstream publishes releases (pin a release instead of a commit), `check_answerable` reaches the REST API,
or conversations are needed for multi-turn analytics.

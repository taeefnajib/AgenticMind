# NVIDIA Auto Ontology adapter

[Auto Ontology](https://github.com/NVIDIA/auto-ontology) (Apache-2.0) ingests a source database, compiles a
semantic layer (business terms, their columns, SQL measures and relationships) and answers questions with a
text-to-SQL agent grounded in that layer. AgenticMind integrates it as an **adapter** (ADR-0019): its
nodes call a deployed instance's public gateway API, and nothing of Auto Ontology is re-implemented.

**Pinned:** upstream commit `778ccdef75536bee5653306f8ded655924e4ada8` (gateway OpenAPI 1.3.0). Upstream
publishes no releases, so the adapter pins a commit; the operations it uses are vendored in
`nodes/semantic/src/agenticmind_nodes_semantic/contract/` and the contract tests check every request and
parsed response against them.

## Nodes

| Node | Does | API |
|---|---|---|
| Auto Ontology | Connection + capability check: reachable, token valid, semantic layer compiled | `GET /api/health`, `GET /api/semantic-compilation/status` |
| Ontology Term Search | Business terms by name | `GET /api/terms` |
| Ontology Term Details | One term: tables, related terms, SQL measures | `GET /api/terms/{id}`, `…/sql-attributes` |
| Ontology Context | Terms a question mentions, as text context (e.g. for NL-to-SQL) | the two above |
| Ask Auto Ontology | Question → answer, SQL, rows and agent steps (streamed) | `POST /api/chat/completions` (SSE) |
| Auto Ontology Tool | Gives an Agent `search_terms` / `ask_question` | the above |

The Ask node's result has the same shape as NL-to-SQL's, so Chat shows the SQL and rows.

Not used: the MCP server (port 3003; AgenticMind's agent tool covers the same ground through the REST API),
and `check_answerable`, which exists only as an MCP tool at the pinned commit.

## Running Auto Ontology locally

Auto Ontology is a service stack (Next.js gateway on :3000, FastAPI backend and ingestion service, Postgres
with pgvector) that needs Docker and NVIDIA model endpoints (an NVIDIA API key from build.nvidia.com).

1. Clone the pinned commit into a folder outside version control (e.g. `.agenticmind/upstream/`),
   `cp .env.example .env`, and set the model keys, new passwords, `AUTH_SECRET` and
   `CONNECTION_STRINGS`. For the sample data, mount a copy of `examples/data/sales.sqlite` read-only at
   `/data` with a `docker-compose.override.yml` for `auto-ontology` and `ingestion-service`, and set
   `CONNECTION_STRINGS=sqlite:////data/sales.sqlite`.
2. `docker compose up -d --build`, open http://localhost:3000, sign in as the admin from `.env`, then
   turn on **Settings → Semantic Compilation** and wait for "Semantic compilation is ready".
3. Mint a token under the user menu → **API Tokens**, and put it in AgenticMind's `.env` as
   `AUTO_ONTOLOGY_API_KEY`.
4. In AgenticMind: **New** → *Ask Auto Ontology*, then **Run → Status** and ask in **Chat**.

Security notes from upstream: always use the gateway (:3000); the internal FastAPI port trusts an identity
header without verifying it. A token acts with its owner's role, so mint it for a least-privileged user.
Without Vault, connection credentials added in the Auto Ontology UI are stored unencrypted in its Postgres.

Troubleshooting: if questions time out and Auto Ontology's logs show embedding retries, NVIDIA's hosted
embedding model may be overloaded. Set `EMBED_MODEL` in Auto Ontology's `.env` to another hosted embedding
model that responds (`nvidia/nemotron-3-embed-1b` worked during testing). Ingest and queries must use the
same model, so recompile afterwards (`POST /api/semantic-compilation/reset`, then restart the services).

## Error codes

`ONTOLOGY_NOT_CONFIGURED`, `ONTOLOGY_UNREACHABLE`, `ONTOLOGY_UNAVAILABLE`, `ONTOLOGY_TIMEOUT`,
`ONTOLOGY_UNAUTHORIZED`, `ONTOLOGY_FORBIDDEN`, `ONTOLOGY_NOT_FOUND`, `ONTOLOGY_TERM_NOT_FOUND`,
`ONTOLOGY_NOT_READY`, `ONTOLOGY_BUSY`, `ONTOLOGY_ANSWER_FAILED`, `ONTOLOGY_API_ERROR`.

## Upgrading the pin

Check out the new commit, run `uv run python scripts/extract_auto_ontology_contract.py <checkout> <commit>`,
update `UPSTREAM_COMMIT`/`API_VERSION` in `auto_ontology.py`, and run the contract tests, then the opt-in live
test (`AGENTICMIND_TEST_AUTO_ONTOLOGY_URL`, `AGENTICMIND_TEST_AUTO_ONTOLOGY_TOKEN`).

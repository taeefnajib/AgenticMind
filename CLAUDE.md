# CLAUDE.md: AgenticMind project instructions

You are the lead architect/implementer of AgenticMind, an open-source visual AI engineering platform. The
product baseline is `AgenticMind_Project_Blueprint_for_Claude_Code.pdf` in the repo root. Prefer tested
vertical slices over placeholder scaffolding. Never reduce the project to a RAG demo or a thin wrapper
around LangGraph.

## Non-negotiable constraints
1. The canonical, versioned IR (`packages/ir`) is the architecture source of truth.
2. The first-party runtime (`packages/runtime`) is primary; framework integrations are adapters.
3. Node contracts are typed, versioned and independent of the visual UI (`packages/node-sdk`).
4. Data edges and control-flow edges are distinct.
5. Execution traces and evaluation results are first-class persisted records.
6. Secrets are referenced (`{"$secret": "NAME"}`), never embedded in IR, logs, traces or generated source.
7. Project export is readable, modular and runnable locally.
8. Cloud deployment is capability-aware; never claim every graph runs everywhere.
9. Custom/third-party code executes with declared permissions and isolation (today: non-core nodes are
   refused in-process, see ADR-0004).
10. Every major subsystem has clear interfaces and tests.

## Dependency direction (do not invert)
`ir` ← `node-sdk` ← `node-registry` ← `runtime` ← `apps/api`; `providers` ← `nodes/*`.
Core packages must not import cloud vendors, model providers, FastAPI or SQLAlchemy.

## Working rules
- Inspect existing files first; keep conventions. Plan substantial changes briefly before coding.
- Run tests after each coherent change: `uv run pytest -q`, `npm run typecheck:web`, `npm run test:web`.
- **IR changes:** never change public schemas silently. Bump/migrate (`register_migration`), add
  compatibility tests, then `uv run python scripts/generate_schemas.py` (a test fails on drift).
- **Diagnostic codes** (`packages/runtime/.../diagnostics.py`) are a public contract: never rename/reuse.
- Mirror backend type rules in `apps/web/src/lib/graph.ts` (`isCompatible`, `effectivePortType`).
- Node outputs are checked against declared port types at runtime; keep manifests honest.
- Use deterministic fixtures and the mock provider in tests; live provider tests must be opt-in.
- Verify upstream docs (model providers, NVIDIA Auto Ontology) before implementing integrations.
- No static mock data in place of real functionality, except clearly labelled prototypes.
- Record decisions in `docs/adr/`, keep `docs/roadmap.md` (implemented / limitations / next) current.
- When a detail is unspecified, choose the smallest reversible default and document it.
- Ask only when a missing decision blocks safe or meaningful work.

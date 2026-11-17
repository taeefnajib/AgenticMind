# Architecture overview

## Subsystems

| Subsystem | Location | Responsibility | Must not depend on |
|---|---|---|---|
| IR | `packages/ir` | Pydantic models of the project graph, published JSON Schema, deterministic serialization, content hashing, migrations, secret-reference helpers | everything else |
| Node SDK | `packages/node-sdk` | Port type system, `NodeManifest`, typed config, `NodeContext`/`NodeResult`, `define_node`, isolated test helpers | registry, runtime, UI |
| Registry | `packages/node-registry` | Discovers node packages (`agenticmind.nodes` entry points), indexes versions, assigns trust | runtime, UI |
| Runtime | `packages/runtime` | Validation pipeline, execution planning, scheduler, retries/timeouts/cancellation, trace events, redaction, CLI | FastAPI, databases, providers |
| Providers | `packages/providers` | Model provider adapter interface (`ModelProvider`); mock provider | runtime, UI |
| Core nodes | `nodes/core` | First-party node package | runtime internals |
| API | `apps/api` | HTTP control plane: projects & revisions, validation, runs, SSE event streams, IR export; persistence (SQLAlchemy) | n/a |
| Web IDE | `apps/web` | Visual architect: node library, React Flow canvas, inspector, console (problems/inputs/trace/output/IR) | n/a |

## Dependency direction

```
ir  ←  node-sdk  ←  node-registry  ←  runtime  ←  apps/api  ←(HTTP)←  apps/web
              ↖  providers  ←  nodes/core  (discovered by registry via entry points)
```

Arrows point at dependencies. Core packages stay free of cloud vendors, model SDKs, web frameworks and
databases, so the same runtime can be embedded in the API, the CLI and (later) generated projects.

## Request flow: "Run"

1. The IDE saves the canvas as a new **revision** (`PUT /api/projects/{id}`). Drafts may be graph-invalid
   but must parse as IR.
2. `POST /api/projects/{id}/runs` loads the revision, runs the **validation pipeline**, compiles an
   **execution plan** (422 + diagnostics if not executable), records a run with an IR snapshot and content
   hash, and starts it.
3. The **engine** schedules nodes, emits redacted **trace events** (persisted with monotonically increasing
   `seq`) and stores node outputs.
4. The IDE subscribes to `GET /api/runs/{id}/events` (SSE), colours nodes by status and shows the trace.
   Reconnection resumes from `Last-Event-ID`.

## Key decisions

See `docs/adr/`: own IR & runtime (0001), monorepo & stack (0002), persistence (0003), node trust &
isolation (0004), in-process run execution (0005), edge semantics (0006).

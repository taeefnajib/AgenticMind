# Runtime: execution semantics and lifecycle

The first-party runtime (`packages/runtime`) executes canonical IR. It has no dependency on web
frameworks, databases or model SDKs; the API, the CLI and future generated projects embed it.

## Lifecycle

```
raw IR ─parse/migrate─▶ ProjectDocument ─validate─▶ ValidationReport
                                         └─compile_plan─▶ ExecutionPlan ─Engine.run─▶ RunResult + TraceEvents
```

### Validation pipeline (`validation.py`)
1. Parse against the IR schema (`IR_*` codes).
2. Resolve node types/versions in the registry (`NODE_UNKNOWN_TYPE`, `NODE_UNKNOWN_VERSION`).
3. Validate each node's config against its schema (`CONFIG_INVALID`, with `path`).
4. Validate edges, ports, types and cardinality (`EDGE_*`, `PORT_*`).
5. Validate structure: duplicates, cycles, isolated nodes (`GRAPH_*`).
6. Check secrets, permissions and trust/isolation (`SECRET_*`, `NODE_ISOLATION_UNAVAILABLE`).
7. Return structured diagnostics: `code`, `severity`, `message`, `node_id`, `edge_id`, `port`, `path`,
   `related_node_ids`, `hint`. Only `error` severity blocks execution.

### Execution plan (`plan.py`)
Typed config is parsed once; per-node data/control inputs, upstream set, effective timeout/attempts and
the set of secrets the node may resolve are precomputed; order is a deterministic topological sort.

## Execution semantics (`engine.py`)

| Rule | Behaviour |
|---|---|
| Readiness | A node is ready when every upstream node (data or control) is terminal. |
| Control routing | With incoming control edges, a node runs only if at least one comes from a succeeded node that activated that branch (OR-join). Otherwise it is **skipped** (`control_not_activated`). |
| Skip propagation | If all producers of a required input were skipped, the node is skipped (`upstream_skipped:<port>`). |
| Joins | `multiple`-cardinality inputs receive a list in **edge-ID order** (not completion order), so results are deterministic. Single inputs accept exactly one edge. |
| Parallelism | Ready nodes run concurrently up to `max_concurrency` (asyncio). |
| Failure | Fail-fast: the first node failure (after retries) cancels in-flight nodes and fails the run. |
| Retries | Only `retryable` errors (`TransientNodeError`), up to `max_attempts`, exponential backoff. Timeouts are retried only for nodes declared `idempotent`. Unhandled exceptions are never retried. |
| Timeouts | Per node: manifest default, overridable in IR `execution.timeout_seconds`. |
| Cancellation | Cooperative via `CancelToken`; running nodes are cancelled and reported. |
| Output contract | Outputs must match declared ports and types exactly; branches must be declared; run outputs must be JSON. |
| Secrets | Nodes resolve only secrets referenced in their own config and only with the `credentials` permission. Values are scrubbed from traces, errors and stored outputs. |
| Cycles | Rejected at validation. Iteration will use explicit Loop nodes. |

### Not yet implemented (documented, not faked)
Durable checkpoints/resume, human-approval suspension points, streaming mode, explicit loop/parallel
nodes, isolated (process/container) execution. The API marks runs interrupted by a restart as
`RUN_INTERRUPTED` instead of pretending to resume.

## Trace events (`events.py`)

Every event: `run_id`, `project_id`, `seq` (1..n, gap-free per run), `timestamp`, `event_type`, and where
relevant `node_id`, `node_type`, `invocation_id`, `attempt`, `retry_count`, `status`, `duration_ms`,
redacted `inputs_summary`/`outputs_summary`, `usage` (provider, model, tokens, estimated cost), `error`
(`code`, `message`, `node_id`, `retryable`), `data`.

Types: `run_started`, `run_succeeded`, `run_failed`, `run_cancelled`, `node_started`, `node_succeeded`,
`node_failed`, `node_retrying`, `node_skipped`, `node_cancelled`, `node_event` (custom, via `ctx.emit`).

Summaries truncate long strings/collections and redact sensitive keys (`api_key`, `token`, `password`…)
and resolved secret values.

### Streaming to clients
`GET /api/runs/{id}/events` returns JSON (`?after=<seq>` for paging) or, with
`Accept: text/event-stream`, an SSE stream: each message has `id: <seq>` and `event: trace`; browsers
reconnect with `Last-Event-ID` and receive only newer events; a final `event: end` is sent once the run is
terminal and all events were delivered. Events are retained with the run (deleted with the project).

## Runtime API (HTTP)

```
POST /api/projects/{id}/validate            validate saved revision or a posted (unsaved) document
POST /api/projects/{id}/runs                start a run (202) or 422 with diagnostics
GET  /api/projects/{id}/runs                list runs
GET  /api/runs/{run_id}                     run record (status, outputs, error, node_status, hash)
GET  /api/runs/{run_id}/events              JSON or SSE
POST /api/runs/{run_id}/cancel              cooperative cancellation
GET  /api/runs/{run_id}/nodes/{node_id}/outputs
```

`POST /runs/{id}/resume` from the blueprint arrives with durable checkpoints.

## Diagnostic codes

| Code | Severity | Meaning |
|---|---|---|
| `IR_INVALID_JSON` | error | Document is not JSON |
| `IR_SCHEMA_VIOLATION` | error | Document violates the IR schema (`path` points at the field) |
| `IR_UNSUPPORTED_VERSION` | error | No migration path to the current IR version |
| `NODE_UNKNOWN_TYPE` | error | Node type not installed |
| `NODE_UNKNOWN_VERSION` | error | Type installed but not that version (`hint` lists available) |
| `NODE_INCOMPATIBLE_IR` | error | Node package does not support this IR version |
| `NODE_ISOLATION_UNAVAILABLE` | error | Node's trust level requires isolation not provided by this runtime |
| `CONFIG_INVALID` | error | Config fails the node's schema |
| `EDGE_UNKNOWN_NODE` | error | Edge references a missing node |
| `EDGE_UNKNOWN_PORT` | error | Missing data port or control branch |
| `EDGE_TYPE_MISMATCH` | error | Incompatible port types; insert a conversion node |
| `EDGE_INVALID_CONTROL_TARGET` | error | Control edge not targeting `trigger` |
| `EDGE_SELF_LOOP` | error | Edge connects a node to itself |
| `EDGE_DUPLICATE` | warning | Same connection twice |
| `PORT_MISSING_REQUIRED_INPUT` | error | Required input not connected |
| `PORT_CARDINALITY_EXCEEDED` | error | Several edges into a single-cardinality input |
| `GRAPH_EMPTY` | warning | No nodes |
| `GRAPH_DUPLICATE_NODE_ID` / `GRAPH_DUPLICATE_EDGE_ID` | error | Non-unique IDs |
| `GRAPH_CYCLE` | error | Accidental cycle (`related_node_ids`) |
| `GRAPH_ISOLATED_NODE` | warning | Node with no connections |
| `SECRET_LITERAL_VALUE` | error | Literal value in a secret field |
| `SECRET_NOT_PERMITTED` | error | Node references secrets without the `credentials` permission |
| `SECRET_NOT_CONFIGURED` | warning | Referenced secret missing from the environment |

Runtime error codes (in `error.code` of events/runs) come from nodes (e.g. `INPUT_MISSING`,
`TEMPLATE_MISSING_VARIABLE`) or the engine (`NODE_TIMEOUT`, `NODE_UNHANDLED_EXCEPTION`,
`NODE_OUTPUT_MISSING`, `NODE_OUTPUT_UNDECLARED`, `NODE_OUTPUT_TYPE_MISMATCH`, `NODE_INVALID_BRANCH`,
`SECRET_ACCESS_DENIED`) or the API (`RUN_INTERRUPTED`, `RUN_ABORTED`, `RUNTIME_INTERNAL_ERROR`).

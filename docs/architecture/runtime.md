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
| Control routing | With incoming control edges, a node runs only if at least one comes from a succeeded node that activated that branch (OR-join; `control_join="all"` nodes such as Join need every one). Otherwise it is **skipped** (`control_not_activated`). Branches may come from config (`branches_from_config`, e.g. Router routes + `default`). |
| Skip propagation | If all producers of a required input were skipped, the node is skipped (`upstream_skipped:<port>`). |
| Joins | `multiple`-cardinality inputs receive a list in **edge-ID order** (not completion order), so results are deterministic. Single inputs accept exactly one edge. |
| Parallelism | Ready nodes run concurrently up to `max_concurrency` (asyncio). |
| Failure | Fail-fast: the first node failure (after retries) cancels in-flight nodes and fails the run. |
| Retries | Only `retryable` errors (`TransientNodeError`), up to `max_attempts`, exponential backoff. Timeouts are retried only for nodes declared `idempotent`. Unhandled exceptions are never retried. |
| Timeouts | Per node: manifest default, overridable in IR `execution.timeout_seconds`. |
| Cancellation | Cooperative via `CancelToken`; running nodes are cancelled and reported. |
| Output contract | Outputs must match declared ports and types exactly; branches must be declared; run outputs must be JSON. |
| Secrets | Nodes resolve only secrets referenced in their own config and only with the `credentials` permission. Values are scrubbed from traces, errors and stored outputs. |
| Cycles | Rejected at validation. Iteration uses paired Loop / Loop End regions (below). |

### Loops (ADR-0011)
A `loop_start` and a `loop_end` node paired by a `scope → scope` edge enclose a body: every node on a
path between them. The plan lifts body nodes out of the top-level order (`ExecutionPlan.regions`). The
Loop End runs the body once per iteration in a fresh scope seeded with `item` / `index` / `state`:
`for_each` (bounded concurrency, results in item order, `on_item_error` fail|skip) or `repeat` (state
carried forward, stop on `continue=false` or `max_iterations`). Events inside a loop carry
`data.iteration` (one index per nesting level). A failure inside an iteration fails the loop with the
inner node's code and an "iteration N" message, unless items are skipped. Values may leave the body
only through Loop End (`LOOP_REGION_LEAK`).

### Run targets
`compile_plan(..., targets=[...])` keeps only the targets and every node they depend on (through data
and control edges). The API accepts `targets` on `POST /runs`; the IDE lists one "Run → …" entry per
Output node. This is how ingestion and query flows in one project run independently.

### Handles and secret delegation
Values on `EmbeddingModel` / `VectorStoreHandle` ports are handles: descriptors that may contain secret
*references*. A node with the `credentials` permission may resolve the references carried by handles
wired into it, in addition to those in its own config. Secret values never travel on edges.

### Checkpoints, suspension and resume (ADR-0013)
Every finished top-level node is checkpointed exactly (`observer.on_checkpoint`); values that are not JSON or
that contain a secret value are marked non-restorable and re-run on resume. A `can_suspend` node raising
`SuspendNode` becomes `waiting`; once independent work is done the run ends as `waiting` (`run_waiting`, with
the requests). `Engine.run(plan, resume=ResumeState(checkpoints, responses, last_seq))` restores the root scope,
passes each response as `ctx.resume_value`, emits `run_resumed` and continues numbering events. Suspension inside
loop bodies is not supported (`SUSPEND_IN_LOOP`).

### Memory (ADR-0012)
Nodes with the `memory` permission get `ctx.memory`, a project-scoped `MemoryAccess` for conversation
turns and long-term memories. The engine takes a `MemoryStore` (`Engine(memory=...)`): the API passes its
database store, the CLI an in-process one that lives for a single run.

### Node storage
Nodes with the `filesystem` permission get `ctx.storage_path(...)` under the runtime data directory (used
for embedded vector databases and the local model cache).

### Not yet implemented (documented, not faked)
Per-iteration loop checkpoints, approval expiry and queue-backed workers, streaming mode, subgraphs, isolated (process/container) execution. The API marks runs interrupted by a restart as
`RUN_INTERRUPTED`; such runs can be resumed explicitly from their checkpoints.

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
GET|DELETE /api/projects/{id}/memories[/{memory_id}]                 long-term memories (?namespace=, ?q=)
GET|DELETE /api/projects/{id}/conversations[/{conversation_id}]      stored conversation turns
```

```
POST /api/runs/{run_id}/resume              continue a waiting run {responses} or an interrupted one {force}
GET  /api/runs/{run_id}/waits               pause points of a run (pending / answered / cancelled)
GET  /api/projects/{id}/approvals           approval inbox (?status=pending|answered|cancelled)
```

A waiting run's SSE stream ends with `event: end` (status `waiting`); clients reconnect after resuming.

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
| `RUN_TARGET_UNKNOWN` | error | A requested run target is not a node in the graph |
| `LOOP_UNPAIRED` | error | Loop without a Loop End connected to its `scope` |
| `LOOP_SCOPE_SHARED` | error | One Loop's scope wired to several Loop Ends |
| `LOOP_REGION_LEAK` | error | A body value goes to a node outside the loop (use Loop End) |
| `LOOP_OVERLAP` | error | Two loops share nodes without nesting |
| `SUSPEND_IN_LOOP` | error | A node that can pause the run (e.g. Human Approval) is inside a loop body |

Runtime error codes (in `error.code` of events/runs) come from nodes (e.g. `INPUT_MISSING`,
`TEMPLATE_MISSING_VARIABLE`, `NO_FILES`, `NO_TEXT_EXTRACTED`, `PARSE_FAILED`, `PARSE_ENCRYPTED`,
`COLLECTION_NOT_FOUND`, `EMBEDDING_DIMENSION_MISMATCH`, `EMBEDDING_MODEL_MISMATCH`, `FILTER_INVALID`,
`PROVIDER_AUTH_FAILED`, `PROVIDER_AUTH_MISSING`, `PROVIDER_INSUFFICIENT_CREDITS`, `PROVIDER_BAD_REQUEST`,
`PROVIDER_UNAVAILABLE`, `MODEL_EMPTY_RESPONSE`, `MODEL_OUTPUT_TRUNCATED`, `PLAN_INVALID`, `PLAN_EMPTY`, `PLAN_EMPTY_GOAL`, `CITATIONS_MISSING`, `STORE_UNAVAILABLE`, `STORE_NOT_INSTALLED`,
`AGENT_LIMIT_REACHED`, `DUPLICATE_TOOL_NAME`, `TOOL_UNKNOWN`, `TOOL_PERMISSION_DENIED`, `LOOP_ITEMS_MISSING`,
`LOOP_ITEMS_INVALID`, `LOOP_TOO_MANY_ITEMS`, `LOOP_ITEM_TYPE_MISMATCH`, `LOOP_STATE_TYPE_MISMATCH`,
`ROUTER_INVALID_REGEX`, `JOIN_INCOMPLETE`, `JOIN_MERGE_NOT_OBJECT`, `MEMORY_UNAVAILABLE`, `MEMORY_INVALID_KEY`,
`MEMORY_EMPTY`, `MEMORY_TOO_LONG`, `AGENT_HISTORY_INVALID`, `APPROVAL_INVALID_RESPONSE`) or the engine (`NODE_TIMEOUT`, `NODE_UNHANDLED_EXCEPTION`, `LOOP_RESULT_NOT_JSON`, `NODE_SUSPEND_UNDECLARED`, `SUSPEND_IN_LOOP`,
`NODE_OUTPUT_MISSING`, `NODE_OUTPUT_UNDECLARED`, `NODE_OUTPUT_TYPE_MISMATCH`, `NODE_INVALID_BRANCH`,
`SECRET_ACCESS_DENIED`, `ARTIFACT_ACCESS_DENIED`, `ARTIFACT_NOT_FOUND`, `STORAGE_UNAVAILABLE`) or the API (`RUN_INTERRUPTED`, `RUN_ABORTED`, `RUNTIME_INTERNAL_ERROR`).

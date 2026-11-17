# ADR-0011: Loops, Router and Join

**Status:** Accepted (8b)

## Context
Agent workflows need iteration (per item, or until a condition holds), multi-way routing and an explicit
"wait for all branches" join. The IR forbids cycles (`GRAPH_CYCLE`) so plans stay analysable, and data
and control edges must stay distinct.

## Decision
- **Loops are paired, acyclic regions.** A `loop_start` node (Loop) and a `loop_end` node (Loop End)
  are paired by a data edge `scope → scope` (port type `LoopScope`). The body is every node that lies on
  a path from the start to the end. There is no back edge: the runtime repeats the region. Manifests
  declare `runtime_role` so the runtime does not special-case node type names.
- **Validation** (`loops.py`): `LOOP_UNPAIRED`, `LOOP_SCOPE_SHARED`, `LOOP_REGION_LEAK` (a body value
  must leave through Loop End) and `LOOP_OVERLAP` (loops must nest properly). Nested loops are supported.
- **Execution:** the plan removes body nodes from the top-level order and records a `LoopRegion` per
  Loop End. The engine runs each iteration in a fresh *scope* (statuses and outputs), seeded with the
  start's `item` / `index` / `state` and sharing values from outside the loop read-only. The same
  scheduler drives the run and each iteration (control routing, skips, retries, fail-fast).
  - `for_each`: one iteration per list item, `concurrency` 1–16, results kept in item order;
    `on_item_error` = `fail` (fail the loop) or `skip` (record in the report, result `null`).
  - `repeat`: sequential; the iteration's value becomes the next state; stops when Loop End's
    `continue` is `false` or after `max_iterations` (≤ 100; engine ceiling 10 000).
  - Loop End outputs `results`, `last` and `report {mode, iterations, stop_reason, errors}`.
  - Every event from inside a loop carries `data.iteration` (a list, one index per nesting level), and
    Loop End emits a `loop_iteration` node event per iteration.
- **Router** declares `branches_from_config="routes"`: its control branches are the configured route
  names plus `default`, which the validator, engine and UI all compute by the same rule
  (`effective_branches`). Rules mode (regex / contains / comparisons, first match wins) is free and
  offline. LLM mode forces a `choose_route` tool call with an enum of route names; an unknown answer
  falls back to `default` and emits `router_fallback`.
- **Join** declares `control_join="all"`: it runs only if *every* incoming control edge was activated
  (the default stays OR). It combines its `multiple` input (collect / merge / concat) and fails with
  `JOIN_INCOMPLETE` below `min_values`.

## Consequences
- Graphs stay acyclic, so validation, run targets, canonical hashing and future compilation are unchanged.
- Loop iterations are not individually checkpointed; durable resume (8d) will need scope snapshots.
- The final status of a body node reflects the last iteration; the IDE shows "ran ×N" from events.
- A parallel fan-out needs no special node: independent branches already run concurrently, and Join
  (AND) or a `multiple` input gathers them.

## Revisit when
Durable checkpoints arrive (8d), subgraphs are introduced, or loops need `break` from deep inside a body.

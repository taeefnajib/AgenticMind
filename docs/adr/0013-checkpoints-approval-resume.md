# ADR-0013: Durable checkpoints, Human Approval and resume

**Status:** Accepted (8d)

## Context
The blueprint requires that "human approval is a durable suspension point, not a worker thread blocked
indefinitely", that long-running jobs use durable checkpoints and resumable execution, and it lists
`POST /api/runs/{run_id}/resume`. Runs previously executed in the API process with no way back after a
restart (`RUN_INTERRUPTED`).

## Decision
- **Checkpoints.** After every top-level node finishes (succeeded or skipped) the engine calls
  `observer.on_checkpoint(NodeCheckpoint)` with the node's *exact* outputs, activated branches and run
  outputs (for a Loop End also its body nodes' statuses). The API upserts them into `run_checkpoints`.
  Values that are not JSON-serialisable or that contain a resolved secret value are not stored
  (`restorable=false`); such nodes are simply executed again on resume. Loop iterations are not
  checkpointed individually.
- **Suspension.** A node declaring `can_suspend` may raise `SuspendNode(request, response_schema)`. The
  engine marks it `waiting`, lets independent branches finish, then ends the run with status `waiting`
  (`node_waiting` / `run_waiting` events). No task, thread or connection remains. The API stores the
  request in `run_waits` (the approval inbox). Suspending inside a loop body is rejected at validation
  (`SUSPEND_IN_LOOP`) and at runtime. Undeclared suspension fails (`NODE_SUSPEND_UNDECLARED`).
- **Resume.** `POST /api/runs/{id}/resume {responses, force}`:
  - *waiting runs*: every response must target a pending wait and satisfy its JSON Schema (422
    otherwise). The run is claimed atomically (`waiting → running`), so a double submit gets 409.
  - *interrupted runs* (`RUN_INTERRUPTED` after a restart): resumable too. If a non-idempotent node had
    started but not finished, the API refuses (409) unless `force=true`, because it could repeat a side effect.
  - The engine rebuilds the root scope from restorable checkpoints, gives each responding node its
    response in `ctx.resume_value`, emits `run_resumed` and continues `seq` from the last stored event.
    The plan is recompiled from the run's IR snapshot and original run targets. Secrets are resolved
    again from the environment.
- **Human Approval node** (`agenticmind.core.human_approval`, category *human*): shows a title,
  instructions and the value; the response has `decision` (approved | rejected), optional `comment`
  (required for rejections by default), `reviewer`, and an edited `value` when `allow_edit` is on. It
  activates the `approved` or `rejected` branch.
- **Cancel** works on waiting runs (`run_cancelled` is appended; pending waits become `cancelled`).
- **IDE**: an Approvals tab (inbox with edit / comment / approve / reject), a "waiting for you" node
  state, chat shows "Paused for approval", and a Resume button for interrupted runs.

## Consequences
- Waiting runs survive restarts (verified by restarting the API while a run waited).
- Resume re-executes any node without a restorable checkpoint; nodes must stay idempotent or declare
  otherwise so the interrupted-run safety check can protect side effects.
- The Agent node cannot pause mid-loop yet (tool-level approval needs agent state checkpoints).
- No approval expiry or reminders (would need a scheduler); no per-user assignment until auth exists.

## Revisit when
Queue-backed workers replace in-process execution, auth adds assignees/roles, or agents need
approval before individual tool calls.

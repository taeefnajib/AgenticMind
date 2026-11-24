# ADR-0039: Durable execution — job queue, leases and automatic recovery

**Status:** Accepted. Feature 7 of the open-source production plan (ADR-0032). Builds on ADR-0005
(the RunManager interface) and ADR-0013 (checkpoints and resume).

## Context
Runs executed as asyncio tasks inside the API process:
- **On restart:** an active run was marked `RUN_INTERRUPTED`, and someone had to resume it by hand.
- **Scaling:** runs could not be spread across processes.
- **Multiple API processes:** several processes would each fire schedules and poll queues (ADR-0035).

ADR-0013 already persisted what a continuation needs: a checkpoint per finished node, waits, and event
sequence numbers.

## Decision

### A job per run, claimed under a lease
- **Tables:** `run_jobs` (one row per run) and `leases` (named leases). They are new tables, so existing
  databases get them from `create_all` without a migration.
- **Job fields:** kind (`start` / `resume`), status (`queued` / `leased` / `done`), owner, lease expiry,
  attempts, targets, responses, `force` and a `cancel_requested` flag.
- **Submitting:** `RunManager.start` writes the run and its job in one transaction. `resume` (approvals,
  interrupted runs) re-queues the job with the responses. The interface of ADR-0005 is unchanged for callers
  (runs, MCP, triggers, evaluations).
- **Claiming** is one conditional `UPDATE … WHERE status='queued' OR (status='leased' AND lease expired)`
  per job, which is atomic on SQLite and PostgreSQL alike, so two workers never hold one job. Each claim
  increments `attempts`.
- **Heartbeat:** a running job renews its lease every `lease_seconds / 3` (default lease 30 s). It also picks
  up `cancel_requested`, and cancels locally when the lease was lost.

### Recovery
- **Expired leases are claimable:** a crashed worker, a killed container or a stopped server simply stops
  renewing. The next claim is a recovery (`attempts > 1`). The worker resumes from the run's checkpoints with
  the existing `ResumeState`: finished nodes are restored, not re-run.
- **Side effects are not repeated silently:** if a non-idempotent node was in flight, the run is failed as
  `RUN_INTERRUPTED` and resumable with `force` (the ADR-0013 guard, now shared as
  `RunManager.unfinished_side_effects`).
- **Capped:** recoveries per run are limited by `max_run_attempts` (3), then `RUN_RECOVERY_LIMIT`.
- **Graceful shutdown:** stop claiming, give running runs a grace period, then **release** their jobs
  (re-queued, run left `running`). Another worker, or the restarted server, continues them immediately. They
  are no longer failed with `RUN_ABORTED`.
- **Old runs:** active runs from before the queue existed (no job) are still marked interrupted at start-up
  (`fail_unqueued_orphans`).

### Workers
- **Embedded:** a worker runs inside the API process (default, `AGENTICMIND_EMBEDDED_WORKER=1`), so a single
  process behaves as before.
- **Separate:** `agenticmind-worker` processes (new console script, `python -m agenticmind_api.worker`)
  execute runs against the same database and data directory. Give the API
  `AGENTICMIND_EMBEDDED_WORKER=0` to leave execution to them.
- **Compose:** the `workers` profile (`--scale worker=N`, with PostgreSQL).
- **Concurrency:** each worker claims at most `max_concurrency` runs and is woken immediately when work is
  submitted locally (otherwise it polls each second).
- **Cross-process cancel:** a queued run is cancelled at once, and a running one through the flag its worker
  sees on the next heartbeat.
- **Cross-process wait:** `wait` polls the database when the run executes elsewhere, which MCP calls, webhook
  `wait` responses and chat replies rely on.
- Finish hooks (metrics, trace export) run in the worker that finished the run.

### One trigger leader
- **Election:** the trigger service takes the `triggers` lease. Only the leader runs schedules and the queue
  and mailbox pollers; it renews the lease on every tick and releases it on shutdown.
- **Everyone else:** every process still accepts webhooks (they only queue runs). This removes ADR-0035's
  double-firing limitation for several API processes.
- **Found by the test:** the service elected only on its first loop tick, 5 s after start. It now elects at
  start.

## Verification
- **Queue primitives:** a claim is exclusive; an expired lease is reclaimed with `attempts=2`; the old owner's
  renewal fails; the cancel flag reaches the owner; named leases are exclusive, renewable and taken over
  after expiry.
- **In-process:**
  - a queued run is cancelled before it starts;
  - a graceful shutdown mid-run releases the job (queued, not failed), and the next process finishes it
    without re-running the checkpointed node.
- **A real worker process killed (`TerminateProcess`) mid-run:**
  - the run stays `running` and owned by the dead worker;
  - a second `agenticmind-worker` reclaims it after the lease and finishes it ("Hello world!");
  - the job shows `attempts=2`, the checkpointed node started once and the in-flight node twice.
- **Across processes:** a cancel from the API process stops a run executing in the worker, and the API
  process waits for a run it did not execute.
- **Triggers:** with two API processes on one database exactly one is the trigger leader, and the other
  takes over when it stops.
- **Regression:** all existing API tests (approvals, interrupted-run resume, triggers, MCP, observability,
  git) pass on the queue.

## Consequences
- **Recovery granularity is a finished node:** a long node interrupted mid-way runs again, or waits for a
  forced resume when it has side effects.
- **Database load:** each running run renews its lease every ~10 s and idle workers poll each second; light
  for SQLite, scalable on PostgreSQL. Several processes need PostgreSQL (or one shared SQLite file on one
  host) and a shared data directory.
- **Not covered:** priority queues, per-project concurrency limits and delayed retries.

## Revisit when
Priorities and fairness, per-project quotas, mid-node checkpoints for long nodes, or an external broker are
needed.

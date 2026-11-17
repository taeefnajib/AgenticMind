# ADR-0005: In-process run execution

**Decision.** The API runs each execution as an asyncio task (`RunManager`) and persists events through a
`RunObserver`. Cancellation uses a cooperative `CancelToken`. On startup, runs left active by a previous
process are marked `RUN_INTERRUPTED` instead of pretending to resume.

**Consequences.** No Redis or worker processes needed for the foundation. Long or CPU-heavy runs share
the API process. Human approval and resume are not possible until durable checkpoints exist.

**Revisit when.** Implementing durable checkpoints, human approval or isolated node workers. The
`RunManager` interface is the seam for a queue-backed implementation (Redis initially, per blueprint).

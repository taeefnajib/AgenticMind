# ADR-0012: Conversation state and long-term memory

**Status:** Accepted (8c)

## Context
The blueprint asks to keep short-term conversation state distinct from long-term memory, to define
storage, retention, retrieval, deletion, isolation and consent, and to never silently persist arbitrary
conversation content. Memory must be inspectable and controllable.

## Decision
- **Two separate stores behind one SDK interface** (`MemoryAccess`, reached via `ctx.memory`, only with
  the new `memory` permission):
  - *Conversation state*: ordered `{role, content}` turns keyed by a client-chosen `conversation_id`.
    Written by **Save Conversation Turn**, read by **Conversation History** (last N turns within a
    character budget) and passed to the Agent's new optional `history` input (Agent 1.1.0; 1.0.0 kept).
  - *Long-term memories*: individual facts in a `namespace` (e.g. per end user), with source run/node,
    metadata and expiry. Written by **Remember** or the Agent's **Memory Tool** in `save` mode; read by
    **Recall** or the Memory Tool in `recall` mode.
- **Consent is explicit in the graph.** Remember has a required Boolean `consent` input; the save tool
  carries `consent` in its handle binding and refuses (the model is told why) unless it is `true`. In
  the IDE the chat sends `memory_consent` from a "Remember facts I share" toggle that defaults to off.
  Conversation turns are stored only where a graph contains Save Conversation Turn.
- **Retention**: every entry has an optional expiry (`retention_days`; 0 = until deleted; defaults 30
  days for turns, 365 for memories). Expired rows are purged whenever a project's memory is read.
- **Isolation**: everything is keyed by project id (the boundary until workspaces/auth exist), then by
  namespace / conversation id. Keys are validated (`[A-Za-z0-9_.:@-]{1,128}`).
- **Retrieval**: BM25-style keyword scoring with light stemming, then the most recent other memories
  fill the remaining `top_k` slots (`match: keyword | recent`). Recency fill exists because keyword
  overlap misses paraphrases ("trip" vs "traveling") and per-user memory sets are small.
- **Storage**: the API persists to `conversation_messages` and `memories` tables (cascade-deleted with
  the project); the CLI and tests use the in-process `InMemoryMemoryStore`.
- **Control**: `GET/DELETE /api/projects/{id}/memories[/{memory_id}]`,
  `GET/DELETE /api/projects/{id}/conversations[/{conversation_id}]`, and a Memory tab in the IDE.

## Consequences
- Memory works offline and deterministically in tests (mock provider calls `save_memory:` / `recall_memory:`).
- No semantic recall yet; an embedding-based ranker can sit behind `MemoryAccess.search` later.
- Secrets in user-provided text are not detected before storage; the save tool's description tells the
  model never to store secrets, and users can delete any entry.

## Revisit when
Workspaces/auth arrive (per-user namespaces derived from identity), semantic recall is added, or
retention needs a background purge job.

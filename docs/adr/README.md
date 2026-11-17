# Architecture decision records

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-own-ir-and-runtime.md) | Canonical IR and first-party runtime; frameworks as adapters | Accepted |
| [0002](0002-monorepo-and-stack.md) | Monorepo layout and starting stack | Accepted |
| [0003](0003-persistence.md) | SQLite default, PostgreSQL via URL, append-only revisions | Accepted |
| [0004](0004-node-trust-and-isolation.md) | Only core nodes execute in-process until isolation exists | Accepted |
| [0005](0005-in-process-run-execution.md) | Runs execute as asyncio tasks in the API process (for now) | Accepted |
| [0006](0006-edge-semantics.md) | Distinct data/control edges, OR-join, skip propagation, fail-fast | Accepted |
| [0007](0007-artifact-storage.md) | Project files in a scoped artifact store, referenced by ID | Accepted |
| [0008](0008-embeddings-vector-stores-run-targets.md) | Embedding providers, vector store adapters, handles, run targets | Accepted |
| [0009](0009-llm-provider-and-grounded-answers.md) | OpenRouter LLM provider, Context/Citation Builder, chat panel | Accepted |
| [0010](0010-tools-and-agent-node.md) | Tool handles, bounded Agent loop, starter tools | Accepted |
| [0011](0011-loops-router-join.md) | Paired loop regions, Router with config-defined routes, AND Join | Accepted |
| [0012](0012-memory.md) | Conversation state vs consented long-term memory, retention, deletion | Accepted |
| [0013](0013-checkpoints-approval-resume.md) | Durable checkpoints, Human Approval suspension, resume | Accepted |
| [0014](0014-multi-agent-patterns.md) | Subagent handoff, Planner, Critic, third-party tool discovery | Accepted |

Template: Context → Decision → Consequences → Revisit when.

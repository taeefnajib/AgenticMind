# ADR-0010: Tool contract and the Agent node

**Context.** Blueprint section 8: agents choose tools dynamically *within declared limits and permissions*;
arguments must be validated, tools run with scoped permissions, and every model request, tool selection
and tool execution must be traced.

**Decision.**
- **Tools are handles.** A tool node outputs a `ToolHandle` on a `Tool` port: `tool_type` (implementation),
  `name`, `description` (what the model reads), JSON-Schema `parameters`, a `binding` (tool config plus any
  handles it needs) and the `permissions` executing it requires. Tools are wired into the Agent's
  `tools` input (multiple cardinality), so the canvas shows exactly what an agent can do.
- **Implementations** are registered per `tool_type` in the agents package (`TOOL_IMPLEMENTATIONS`).
  Smallest reversible default; an `agenticmind.tools` entry-point group can follow for third-party tools.
- **Secrets** stay references. Handle-type inputs delegate secret references *transitively*, so an agent
  may resolve the embedding key carried through a Knowledge Search tool, but nothing else.
- **Agent loop** (`agenticmind.agents.agent`): model turn → tool calls → validated execution → results back
  to the model, until a final answer. Limits: `max_turns`, `max_tool_calls`, `token_budget`,
  `cost_budget_usd` (using provider-reported cost). On a limit the node fails with `AGENT_LIMIT_REACHED`
  (default) or returns a partial answer (`on_limit: return_partial`). Unknown tools, schema violations,
  tool errors and per-tool timeouts are returned to the model as tool results so it can recover; they are
  not node failures. The node is never retried automatically (`max_attempts=1`, not idempotent), because
  tools may have side effects.
- **Starter tools:** Knowledge Search (reuses retrieval), Calculator (AST evaluator, no `eval`), Date & Time,
  Web Fetch (explicit domain allowlist required; blocks non-public addresses including cloud metadata;
  re-checks every redirect; size and time limits).
- **Observability:** `agent_turn` and `tool_call` trace events, aggregated usage/cost on the node, and
  `result.steps` / `report` / `transcript` outputs; the chat panel shows the tool steps.

**Consequences.** Agents are bounded, auditable and use the same handles as workflows. Tool permissions are
checked against the Agent's declared permissions, but tools still run in-process (core trust only);
process isolation for third-party tools remains future work (ADR-0004). No conversation memory yet (8c),
and the agent cannot pause for human approval yet (8d).

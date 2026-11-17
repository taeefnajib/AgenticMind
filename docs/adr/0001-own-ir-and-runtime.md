# ADR-0001: Canonical IR and first-party runtime

**Context.** The blueprint requires that AgenticMind not become a drag-and-drop wrapper around LangGraph or
any other framework, and that users keep their architecture and code.

**Decision.** A versioned, framework-independent IR (`packages/ir`) is the only persisted representation
of architecture. A first-party engine (`packages/runtime`) executes it. Framework integrations will be
*adapters* that translate the IR (or parts of it) into framework constructs.

**Consequences.** We own scheduling, state, retries and traces, which costs more up front. In return
semantics are stable across adapters, traces are uniform, and export does not depend on a framework's
data model. Adapters may support only a subset of graphs and must declare it.

**Revisit when.** Never for the source-of-truth question; adapters are added per demand.

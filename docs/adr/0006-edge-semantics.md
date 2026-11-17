# ADR-0006: Edge and execution semantics

**Decision.**
- Data edges connect typed output ports to input ports; control edges connect a source *branch* to the
  target's implicit `trigger` port. Ordinary nodes have one branch, `done`.
- A node with control inputs runs if **any** incoming control edge is active (OR-join); otherwise it is
  skipped. Skips propagate through required data inputs.
- `multiple` inputs receive values in edge-ID order (deterministic join); single inputs accept one edge.
- Accidental cycles are rejected; iteration will use explicit Loop nodes with declared limits.
- Fail-fast on node failure; retries only for retryable errors; timeouts retried only if idempotent.
- Type compatibility is strict with subtype acceptance into `Any`/`JSON`; otherwise explicit conversion
  nodes are required.

**Consequences.** Deterministic, explainable runs. AND-joins over control flow and "continue on error"
policies are not yet expressible; they will arrive as explicit Join/Parallel nodes and per-node error
policies rather than implicit behaviour.

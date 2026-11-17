# ADR-0014: Multi-agent patterns and third-party tools

**Status:** Accepted (8e)

## Decision
- **Supervisor / handoff**: the *Subagent* node offers an agent (instructions, model, tools, limits) as a
  tool. A supervisor Agent delegates by calling it with `{task, context}`: an explicit, traced handoff
  (`handoff` / `handoff_result` events; inner events labelled `supervisor > subagent`). Depth is capped
  at 3 agents, self-delegation is refused, subagent failures return to the supervisor as tool errors,
  and the subagent's tokens and cost count toward the supervisor's budgets. Secret references flow along
  tool edges (values never do).
- **Planner / worker**: the *Planner* node returns at most `max_tasks` self-contained tasks via a forced
  `submit_plan` tool call; a Loop runs workers (in parallel) and an agent synthesises the results.
- **Critic / reflection**: the *Critic* node runs deterministic rule checks (word limits, required or
  forbidden phrases) separately from model-judged criteria, and reports them separately; it passes only
  if all rules and all required criteria pass. Loop End 1.1.0 adds `next_state`, so a repeat loop can
  collect drafts while feeding the critic's revision request into the next round (1.0.0 kept).
- **Third-party tools**: implementations are discovered from the `agenticmind.tools` entry-point group.
  Built-in tool types cannot be replaced; broken or non-async plugins are skipped and recorded. A plugin
  implementation is only reachable through a handle from a tool *node*, and non-core nodes stay refused
  unless the operator trusts them (ADR-0004).
- The mock provider fills tool arguments from the JSON Schema, so all patterns run offline in tests.

## Consequences
- Patterns are composed from ordinary nodes and stay visible on the canvas and in traces.
- Subagents run in-process inside the supervisor node; per-subagent checkpoints are not stored.

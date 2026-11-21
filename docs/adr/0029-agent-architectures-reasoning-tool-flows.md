# ADR-0029: Agent architectures, reasoning strategies, tool routing and tool chaining

**Status:** Accepted. This is step 5 of the "build any agentic workflow" plan (steps 1–4 are ADR-0025 to
ADR-0028).

## Context
Users asked for agent architectures such as ReAct; reasoning methods (chain of thought, tree of thoughts,
graph of thoughts, task decomposition, Monte Carlo tree search, backtracking); and function calling, tool
routing and tool chaining.

The Agent node (ADR-0014) already ran a bounded native function-calling loop, with schema-checked tool
arguments, timeouts and turn / tool-call / token / cost limits. The Planner node already decomposed goals
for loops of workers.

## Decision

### Agent architectures (`architecture` on the Agent, defaulting to the old behaviour)
- **`function_calling`:** the native tool-call loop. The tool calls of one turn now run concurrently
  (`parallel_tool_calls`, default on); results return to the model in call order.
- **`react`:**
  - The model writes `Thought:` / `Action:` / `Action Input:` (JSON) or `Final Answer:`, and the runtime
    executes the action and replies `Observation: …`.
  - Tools are described in the prompt, so it works without native tool calling, and every thought is a
    trace event (`agent_thought`) and part of the step.
  - A malformed reply gets one corrective observation instead of failing the run.
- **`plan_and_execute`:**
  - One call writes up to `max_plan_steps` steps (JSON, with a numbered-list fallback).
  - Each step runs as a function-calling loop that sees the results of earlier steps, and gets what is left
    of the run's budgets.
  - A final call composes the answer from the step results.
- All three share the Agent's checks and limits and `on_limit` (fail or return a partial answer).
- **Chat** shows ReAct thoughts and plan steps, with nested tool calls.

### Reasoning node (`agenticmind.agents.reasoning@1.0.0`)
- **Methods:**
  - `chain_of_thought`, where `samples` > 1 adds self-consistency (a majority vote on normalised answers);
  - `decomposition` (least-to-most sub-questions);
  - `tree_of_thoughts` (propose `branching` steps per state, score them in one call, keep `beam_width`, up
    to `depth`);
  - `graph_of_thoughts` (generate solutions, score, aggregate the best into a node with several parents,
    refine while the score improves);
  - `mcts` (UCT selection, model-proposed expansion, a model-estimated value as the simulation result,
    back-propagation; the answer follows the explored path with the best mean value);
  - `backtracking` (depth-first search with a validity check per step: rejected steps try the next
    alternative, dead ends back up).
- **Budget:** `max_llm_calls` and `token_budget` bound the search. When they run out, the search stops and
  the best path found so far is answered from, using one final call allowed past the limit; the result is
  marked `stopped_early`.
- **Outputs:** `answer`, `reasoning` (the chosen path) and `result`, which holds the whole explored tree
  (ids, parents, text, score, visits, status) plus the call count. Proposing uses the configured temperature;
  scoring and checking use 0.

### Tool Router (`agenticmind.agents.tool_router@1.0.0`)
- **Choosing the tool:** one request → one tool, chosen by keywords (connection order), by the model
  (function calling with `tool_choice="required"` over all tools), or keywords then the model; with
  `default_tool` as the fallback.
- **Arguments:** the model fills the chosen tool's arguments, and the tool runs through the Agent's
  checked executor.
- **Outputs:** result, tool name and text.

### Tool Chain (`agenticmind.agents.tool_chain@1.0.0`)
- **Steps:** a fixed sequence of tool calls. Arguments are templates over `{{input}}`, `{{previous}}`,
  `{{steps.N.result}}` and dotted paths.
- **Types are kept:** a string that is exactly one template keeps the value's type.
- **Errors:** stops at the first failure unless `stop_on_error` is off.
- **Outputs:** the last result and every step.

### Template
**Think it through (reasoning)** (tree of thoughts, with the explored tree as an output).

## Verification
- **Scripted-model tests:**
  - ReAct parsing (fences, trailing observations, bad JSON) and a loop that recovers from a malformed
    reply;
  - plan-and-execute passing earlier results to later steps; parallel tool calls keeping their order;
  - every reasoning method choosing the good branch, with ToT pruning, backtracking rejections, a GoT
    aggregation with two parents and MCTS visits;
  - the budget stopping a search while still answering;
  - Tool Router by keyword (real calculator), by the model, and failures;
  - Tool Chain feeding results forward and template errors.
- **Bugs found and fixed by these tests:**
  - the ReAct parser failed on a fenced input followed by an observation;
  - MCTS picked the most-visited child even when its sibling had a better mean value.
- **Live (OpenRouter / Gemini):**
  - ToT, self-consistency and MCTS answered the bat-and-ball puzzle correctly (0.05);
  - ReAct used the calculator (17.5 % of 2480 = 434);
  - plan-and-execute returned 1444;
  - the Tool Router chose the calculator for 2^16 = 65536.

## Consequences
- **Cost:** search methods trade model calls for quality. Defaults stay small (40 calls), and the explored
  tree makes the cost visible.
- **Approximations:** MCTS uses model value estimates instead of full rollouts, and backtracking relies on the
  model's step checks. Both are only as good as the model's judgement.
- **Not covered yet:** reasoning strategies inside the Agent loop itself (for example ToT over tool use) and
  learned routers.

## Revisit when
Tool-using tree search, verifier models, cached evaluations or streamed thoughts are needed.

# ADR-0030: More reasoning methods, Classifier and Summarizer

**Status:** Accepted. An addition to step 5 (ADR-0029), before step 6 (guardrails).

## Context
After step 5 the user asked for other methods an AI engineer commonly needs. The Reasoning node covered search
methods (tree/graph of thoughts, MCTS, backtracking) but not the iterative-improvement and ensemble patterns
that are now standard, and the catalogue had no dedicated nodes for two very common tasks: classifying text
and summarising long documents (both were possible only through a hand-prompted LLM node).

## Decision

### Reasoning node: six more methods (same node type and version; a compatible enum addition)
- **`self_refine`** (Madaan et al., 2023): draft → critique (`issues`, `good_enough`) → revise, for up to
  `rounds` revisions; stops early when the critique finds nothing.
- **`reflexion`** (Shinn et al., 2023): attempt → self-evaluation (`correct`, `score`, `feedback`) → a one-line
  lesson → retry with all lessons. Returns the first correct attempt, else the best-scored one.
- **`debate`** (Du et al., 2023): `agents` debaters (or one per entry in `models`, same provider) answer, then
  for `rounds - 1` rounds read the others and revise. A strict majority of normalised answers wins; otherwise a
  neutral judge call decides.
- **`mixture_of_agents`** (Wang et al., 2024): proposers answer in `rounds` layers, each layer seeing the
  previous one; an aggregator call synthesises the last layer.
- **`step_back`** (Zheng et al., 2023): first name the underlying principle, then solve with it.
- **`program_of_thought`** (Chen et al., 2022 / PAL): the model writes one arithmetic expression, evaluated by
  the existing safe calculator evaluator (literals, arithmetic and whitelisted math functions; no `eval`, no code
  execution). An expression that does not evaluate is rejected and the model gets up to two corrections.
- All share the node's budgets (`max_llm_calls`, `token_budget`), the explored-thought tree and the trace.

### Classifier (`agenticmind.core.classifier@1.0.0`)
- Labels (`name`, `description`, optional few-shot `examples`); each label is a control branch, plus `default`.
- Single or multi-label (`multi_label`: every matching branch runs), via a forced `classify` tool call with an
  enum (`none` allowed), and a text fallback that accepts a label named in the reply.
- `min_confidence`: below it the input goes to `default`, with the reason recorded.
- Outputs `label`, `labels`, `confidence`, `reason`. Differs from the Router's LLM mode by multi-label,
  confidence, examples and typed outputs; the Router stays for rule-based routing.

### Summarizer (`agenticmind.core.summarizer@1.0.0`)
- Input: text, a Document or a DocumentSet (documents are joined).
- Strategies: `stuff` (one call), `map_reduce` (summarise chunks, then combine; repeated while the combined
  partials are longer than a chunk, and stopped when another level would not shrink them), `refine` (a running
  summary updated chunk by chunk), `auto` (stuff if it fits one chunk, else map-reduce).
- Chunks cut at paragraph, then line, then sentence, then word boundaries; `max_chunks` refuses oversized inputs
  instead of paying for them. `style` (paragraph / bullets / TL;DR / detailed), `max_words`, `focus`.
- Usage is summed over all calls (cost unknown if any call's cost is unknown).

### Output token caps
Live tests showed Gemini spending small `max_tokens` budgets on hidden reasoning and returning nothing. Both new
nodes take a `max_tokens` (default 2000), and the Router's LLM mode cap went from 200 to 1024.

### Template
*Classify and summarise feedback* (multi-label Classifier + bullet Summarizer).

## Verification
- Scripted-model tests for every new method (refinement stops when the critique passes, lessons reach the
  retry, each debate model is used and the majority wins, two MoA layers, the principle comes first, rejected
  expressions retried), Classifier (branches, multi-label filtering of unknown labels, confidence threshold,
  `none`, text fallback, config validation) and Summarizer (all strategies, usage sums, boundary splitting,
  `max_chunks`, termination with tiny chunks). The splitter preferring the latest boundary of any kind (a
  sentence end over an earlier paragraph end) was found and fixed by these tests.
- Live (OpenRouter / Gemini): all six methods answered the bat-and-ball puzzle (0.05) in 1-6 calls; the
  Classifier labelled the template's feedback `bug` + `churn_risk` (0.95); the Summarizer produced bullets, and
  map-reduce summarised the README in 3 chunks.

## Consequences
- Ensemble methods multiply calls by the number of agents and rounds; defaults stay small (3 agents, 2 rounds).
- Debate and mixture across models are limited to models of one provider.
- Program of thought covers arithmetic only; general code execution needs the sandbox (ADR-0004) first.

## Revisit when
Cross-provider ensembles, code-executing program of thought in a sandbox, or embedding-based (zero-cost)
classification are needed.

# ADR-0017: Evaluation Lab

**Status:** Accepted (M5)

## Context
The blueprint makes evaluation first-class. It calls for versioned datasets, versioned evaluators,
evaluation runs, experiments and baselines; metrics for retrieval, generation, safety, operations and
agents; and the cautions that LLM judges are estimates (store model, prompt and rubric), aggregation is
disclosed, comparisons use the same dataset version, and raw per-case results are preserved. M5
acceptance: run the same dataset against two versions with auditable per-case results.

## Decision
- **Core package `agenticmind-evaluation`** (no FastAPI or SQLAlchemy): `TestCase` (inputs, reference,
  expected sources, must / must-not include phrases, expected tools), `DatasetContent` (content hash),
  an evaluator registry, an LLM `Judge`, `run_evaluation` and `compare`.
- **Every case is a real run.** The runner compiles the plan for the answer Output (run target) once, then
  runs each case through the engine with its own run ID and trace (bounded concurrency). In the API, case
  runs are ordinary persisted runs, so any score can be traced back to the execution, events and outputs
  behind it. The scored result (metrics with details, plus the observation: answer, citations, retrieved
  passages, tools used, latency, tokens, cost, retries) is stored per case.
- **What gets scored:** the answer comes from the chosen Output (a string, or an answer object
  `{answer, citations, validation, steps}`). Retrieval metrics score the `documents` of the last retrieval
  node the answer depends on (Retriever, Keyword/Hybrid Search, Reranker, Context Compressor), or a node
  named in the config. Expected sources match by chunk id, or by filename plus a section substring.
- **Evaluators (versioned):**
  - retrieval: Recall@K, Precision@K (over results returned, at most K), MRR, NDCG@K (binary relevance),
    hit rate;
  - generation: required facts, token F1 vs reference, citation validity, cited expected sources, plus
    correctness and groundedness LLM judges;
  - safety: forbidden content;
  - agent: expected tools used (including inside subagents);
  - operations: success, latency (mean, p50, p95), tokens, cost, retries.

  A metric that does not apply to a case is `null` and excluded from the mean, so summaries show
  `n/of`. Each summary row states its aggregation method.
- **Judges are estimates.** A forced `submit_grade` call scores 1-5 against a stated rubric, scaled to
  0-1. Every grade stores the judge model, prompt version (`correctness-v1`, `groundedness-v1`), rubric
  and reasoning; the run stores judge usage and cost. A judge failure is "not applicable" with the error,
  never a silent 0. The UI marks judge metrics as "estimate".
- **Datasets are immutable per version.** Saving appends a version unless nothing changed. An evaluation
  records dataset id, version and content hash, the project revision and IR hash, and the full config.
  `compare` reports per-metric deltas, per-case changes (regressions and improvements) and cases missing
  from either side. It flags runs as not comparable when the dataset version or hash, K or the judge
  differ. One run per project and dataset can be the **baseline**.
- **UI:** an Evaluation Lab overlay with Datasets (form editor, JSON import/export), Run (dataset version,
  graph revision, label, answer output, K, evaluators by area, judge model and secret name) and Results
  (summary with methods, per-case table with answer, retrieved passages and metric details, baseline,
  compare). A sample dataset for the demo handbook lives in `examples/datasets/`.

## Verification
Live with Gemini on the Advanced RAG demo: the 8-case handbook dataset ran against revision 2
(cross-encoder reranker) and revision 3 (weak word-overlap scorer). Both reached 100% recall, MRR,
required facts and judge correctness, with 0 regressions; the differences were latency (+0.4 s) and cost.
On a 9-chunk corpus, hybrid search already ranks the right chunk first, so the reranker adds nothing.
Live testing also led to `citation_validity` 1.1: citing nothing is not applicable when a case expects no
sources (unanswerable questions).

## Consequences
- Evaluations cost real model calls (about $0.002 per case for the pipeline, plus the judge); the Run tab
  makes the judge opt-in.
- Case runs appear in the project's run history like any other run.
- Statistical significance is not computed; small datasets show direction, not proof.

## Revisit when
Datasets need generation from documents, significance testing or CI gating against the baseline is wanted,
or agent trajectories need richer scoring (handoffs, approvals).

# Evaluation Lab

Measure a graph against a versioned dataset, and compare two versions of the graph on the same dataset
(ADR-0017). In the IDE: **Evaluate**. Try it with `examples/datasets/employee-handbook-qa.json` on the
*Advanced RAG* template (upload `examples/data/employee-handbook.md`, run **Index report** first).

## Test case

```json
{"id": "hotel_limit",
 "inputs": {"question": "What is the hotel limit per night?"},
 "reference": "Up to 180 EUR per night in major cities, 120 EUR elsewhere.",
 "expected_sources": [{"filename": "employee-handbook.md", "section": "Travel"}],
 "must_include": ["180"], "must_not_include": [], "expected_tools": []}
```

`expected_sources` entries can also be written `"file.md#Section"` or a chunk id (`art_…#c3`). Every field
except `id` and `inputs` is optional; metrics that need a missing field are *not applicable* for that case.

## Evaluators

| Area | Evaluator | Needs |
|---|---|---|
| retrieval | `recall_at_k`, `precision_at_k`, `mrr`, `ndcg_at_k`, `hit_rate` | expected_sources + a retrieval node |
| generation | `contains_required` | must_include |
| generation | `reference_f1` | reference |
| generation | `citation_validity`, `citation_source_recall` | an answer object with citations |
| generation | `correctness_judge`, `groundedness_judge` (estimates) | reference / retrieved passages + judge |
| safety | `forbidden_content` | must_not_include |
| agent | `tool_use` | expected_tools |
| operations | `success`, `latency_ms`, `tokens`, `cost_usd`, `retries` | — |

`GET /api/evaluators` returns versions, descriptions and aggregation methods.

## API

```
GET|POST /api/projects/{id}/datasets            list / create
GET|PUT|DELETE /api/datasets/{id}[?version=]    read / save new version / delete
GET  /api/datasets/{id}/versions
POST /api/projects/{id}/evaluations             {dataset_id, dataset_version?, revision?, label, config}
GET  /api/projects/{id}/evaluations
GET  /api/evaluations/{id}  ·  /cases  ·  POST /cancel  ·  PUT /baseline  ·  DELETE
GET  /api/evaluations/compare?a=…&b=…
```

`config`: `{"evaluators": [...], "answer_output": "answer", "retrieval_node": null, "k": 5,
"concurrency": 2, "judge": {"provider": "openrouter", "model": "google/gemini-3.8-flash",
"api_key": {"$secret": "OPENROUTER_API_KEY"}}}`.

Each case's `run_id` is a normal run: `GET /api/runs/{run_id}/events` shows exactly what produced the score.

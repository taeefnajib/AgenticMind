# ADR-0015: Advanced retrieval (keyword index, hybrid search, reranking, rewriting, compression)

**Status:** Accepted (step 4)

## Decision
- **Keyword index next to the vector collection.** Index Documents 1.1.0 (`keyword_index`, default on)
  also writes every chunk to a local SQLite FTS5 index (BM25, Porter stemming, Unicode folding) keyed by
  backend + collection + namespace, with the full document JSON and the vector stores' filter fields. It
  is synced by content hash independently of the vectors, so collections indexed before 1.1.0 gain a
  keyword index on the next run without re-embedding. 1.0.0 is kept. Free text becomes a quoted OR-query,
  so users never control FTS syntax. The index is local even for remote vector backends.
- **Keyword Search** (BM25 only) and **Hybrid Search** (vector + keyword, one or more query variants).
  Fusion is reciprocal rank fusion by default (scale-free) or a weighted mix of min-max-normalised scores.
  If the keyword index is missing, Hybrid Search degrades to vector results and emits
  `keyword_index_missing`. The `retrieved` event records each top result's vector and keyword rank.
- **Reranker**: a local cross-encoder via fastembed by default (`Xenova/ms-marco-MiniLM-L-6-v2`, ~80 MB,
  no key), with logits mapped to 0..1 by a sigmoid; or an LLM scoring passages 0-10 through a forced tool
  call; or a deterministic `overlap` scorer for tests. It sets `rerank_score` and keeps the first-stage `score`.
- **Query Rewriter**: one forced tool call returns a standalone query (resolving references with
  optional chat history) plus N variants for Hybrid Search. It falls back to the original query rather
  than blocking retrieval.
- **Context Compressor**: extractive by default (BM25-scored sentences, neighbours, original order, `…`
  between gaps). LLM mode keeps only sentences that literally occur in the source, so compression cannot
  introduce text. IDs and sources are kept; offsets are dropped because they no longer describe the
  shortened text.
- Model-calling nodes default to 4000 output tokens because reasoning models spend part of it thinking.
  Truncation is reported as `MODEL_OUTPUT_TRUNCATED`.

## Consequences
- The *Advanced RAG* template wires the full pipeline; the earlier RAG templates now build keyword indexes too.
- On the sample handbook (9 chunks) vector search already ranks well. The reranker mainly adds a clear
  confidence signal (0.2-0.99 for the right section, ~0 for unanswerable questions); hybrid helps with
  exact terms (codes, names). Thresholds are left off by default because paraphrased questions can score
  low with the small cross-encoder.
- Deleting source documents does not yet remove their chunks from either index (as before).

## Revisit when
Large corpora need a server-side lexical index (e.g. OpenSearch, pgvector + tsvector), evaluation (M5) can
measure retrieval quality per configuration, or stale-chunk cleanup is added.

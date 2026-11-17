# ADR-0009: Real LLM provider and grounded, cited answers

**Context.** Step 3 of the RAG slice: answer questions from retrieved chunks with verifiable citations,
using a real model (user decision: OpenRouter, Gemini by default). Agents (blueprint section 8) will need
the same provider with tool calling.

**Decision.**
- **Provider layer.** `OpenAICompatibleChat` (`packages/providers/chat.py`) implements OpenRouter
  (`openrouter`, base URL `https://openrouter.ai/api/v1`) and any OpenAI-style endpoint
  (`openai_compatible`). Verified against OpenRouter's API reference (Nov 2026): Bearer auth,
  `max_completion_tokens`, OpenAI-style `tools`/`tool_calls`, `usage.cost` in USD. Errors are classified
  (auth, insufficient credits, retryable 429/5xx, bad request). Tool calling is part of the request and
  response types now, so the Agent node can use it without a provider change.
- **Default model** `google/gemini-3.8-flash`: newest stable (non-preview) Gemini Flash on OpenRouter at
  the time, tool-capable and inexpensive. It is a plain config value.
- **LLM node 1.1.0** (OpenRouter default, `system` input, empty-response detection). **1.0.0 stays
  registered** unchanged so existing projects keep validating and running; the node library only offers
  the latest version of each type.
- **Context Builder** numbers sources `[1]..[n]`, deduplicates identical/overlapping chunks, keeps them
  within an approximate token budget (always at least one source, truncated if needed) and renders the
  prompt from an editable template that must contain `{{context}}` and `{{question}}`.
- **Citation Builder** parses `[n]`, `[1, 3]` and `[1-3]`, maps them to sources, removes markers that point
  to non-existent sources (configurable), can require at least one citation, and outputs
  `{answer, citations, validation}` for the chat panel and APIs.
- **Chat panel** runs the selected output as a run target per question (single turn; conversation memory
  arrives with section 8). Runs and chat share one execution path, so traces and node status appear for
  chat questions too.

**Consequences.** Grounded answers with auditable sources and per-question cost. Citation validation
checks that cited sources exist, not that the cited text supports the claim; faithfulness evaluation
belongs to the Evaluation Lab. The mock provider cites source 1 so the pipeline is testable offline.

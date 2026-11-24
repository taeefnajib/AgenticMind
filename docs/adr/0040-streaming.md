# ADR-0040: Streaming model output

**Status:** Accepted. Feature 8, the last of the open-source production plan (ADR-0032).

## Context
Model answers appeared only when a node finished. Chat felt slow, and applications built on AgenticMind could
not offer the token-by-token experience users expect. Runs may execute in a different worker process
(ADR-0039), so streaming cannot rely on in-memory callbacks between the runner and the client.

## Decision

### Providers
- **`chat_stream(provider, request, …, on_text)`** returns the usual `ChatResponse` and calls `on_text`
  with text deltas.
- **Providers with a `stream()`:**
  - **OpenAI-compatible** (OpenRouter, OpenAI, Azure, Mistral, Groq, DeepSeek, Together, xAI, Hugging Face,
    Ollama):
    - `stream: true` plus `stream_options.include_usage`;
    - an SSE parser that ignores keep-alive comments;
    - an accumulator that rebuilds content, tool calls from their index-keyed fragments (id, name, argument
      pieces), the finish reason, usage and cost.
  - **Anthropic:** the official SDK's `messages.stream` (`text_stream`, then `get_final_message`, so thinking
    blocks and usage are kept), with the beta variant when server-side fallbacks apply.
  - **Gemini:** `:streamGenerateContent?alt=sse`. Partial responses are merged; thoughts are not streamed
    but all parts are kept for replay.
  - **Mock:** word by word, for tests and demos.
- **Fallback:** a provider without `stream()` (Bedrock's binary event stream, test fakes) delivers the whole
  text as one delta, so callers need no special cases.

### Nodes and runtime
- **SDK:** `NodeContext.stream_text(delta)`, plus `ctx.streaming`, which tells nodes whether anyone listens.
- **Engine:** gives each invocation a sink when the observer implements
  `on_stream(run_id, node_id, invocation_id, delta)`. Other observers (CLI, tests) get no sink, and nodes
  then make normal non-streaming calls.
- **Streaming nodes:** the LLM node, and every Agent turn including the wrap-up, stream when someone listens.
- **Deltas are a live preview:** node outputs remain the source of truth, and the deltas of a node add up to
  its output (verified).

### Delivery across processes
- **Storage:** `DbObserver` buffers deltas and writes batches to `run_stream_chunks` (run, seq, node,
  invocation, text). It flushes every 100 ms or 256 characters, and always before the node's next trace
  event, so text lands before the event that ends its node. Any API process can then serve them, wherever
  the run executes.
- **Run event stream:** the SSE stream (`GET /api/runs/{id}/events`) adds `event: token` messages with the
  chunk `seq`, so a reconnecting client drops replays. `?tokens=false` leaves them out. Finished runs replay
  their tokens.
- **`POST /api/projects/{id}/runs/stream`:** one call for applications: `event: run` (id), then `token`
  deltas and `trace` events (`?trace=false` to skip), then `result` (status, outputs, error) and `end`.
- **IDE:** the Chat panel shows the answer being written (with a cursor) instead of "Thinking…", then the
  final answer.

### Upstream docs
- **Formats:** these are the providers' published streaming formats (OpenAI-style `chat.completion.chunk`
  SSE with `[DONE]`, Anthropic's SDK stream helpers, Gemini's `alt=sse`).
- **Verified live** against OpenRouter and in tests through the Anthropic SDK with Anthropic's event format.

## Verification
- **Providers:**
  - tool calls rebuilt from argument fragments;
  - OpenAI-compatible streaming with usage and cost (request flags checked); a tool-call stream;
  - Gemini streaming with thoughts excluded and parts kept;
  - Anthropic through the real SDK fed `message_start` … `message_stop` events;
  - the one-delta fallback; mock word-by-word; no listener means a normal call.
- **Engine:** LLM and Agent deltas reach a streaming observer and add up to each node's output; plain
  observers run without streaming.
- **API:**
  - the one-call endpoint emits run, token, result and end (no traces with `trace=false`);
  - joined tokens equal the output;
  - ordered seqs;
  - a run's event stream replays tokens, and leaves them out with `tokens=false`.
- **Found by the tests:** a test helper that wrapped the HTTP client twice. This was only in the tests.
- **Live:**
  - OpenRouter / Gemini 3.8 Flash streamed 8 incremental deltas about 100 ms apart. This model thinks for
    about 26 s first (3,996 output tokens, mostly reasoning), so visible text arrives in under a second at the
    end;
  - through the API endpoint, the joined tokens matched the answer;
  - in the IDE, a Llama 3.3 70B chat visibly typed out while the Writer node was running, then showed the
    final answer.

## Consequences
- **Latency:** delivery is batched (≈100 ms) and polled (≈200 ms), so smooth "typing" rather than per-token
  latency. An in-memory fast path for runs in the same process would cut that further.
- **Extra stored rows:** one row per batch. They are kept with the run and deleted with it.
- **Not streamed yet:** Bedrock (one chunk), reasoning-strategy and guardrail nodes, MCP tool results, and
  exported standalone apps.

## Revisit when
Sub-100 ms streaming, Bedrock's event stream, streaming in exported apps or over MCP, or streamed structured
outputs are needed.

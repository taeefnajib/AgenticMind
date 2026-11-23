# ADR-0036: Native model providers

**Status:** Accepted. Feature 4 of the open-source production plan (ADR-0032).

## Context
Real models were reachable through OpenRouter, Hugging Face, Ollama or any OpenAI-compatible URL. Teams often
must use their own provider contracts (OpenAI, Anthropic, Google, Azure, AWS) for billing, data-processing
agreements, regional hosting or provider-specific features. Two needs follow from that:
- **Provider-specific behaviour:** Claude's prompt caching, thinking and refusal fallbacks; Gemini's thought
  signatures.
- **Without a proxy:** the provider's own API, not a third-party gateway.

## Decision

### Providers (`ChatProviderName`)
- **Adding names is compatible:** these are new enum options in every model-using node's config, so node
  versions are unchanged.
- **One info table, `PROVIDERS`:** label, usual key secret, what `base_url` means when it is required, and a
  default-model hint. It drives:
  - node config validation (`check_chat_provider`);
  - the assistant settings, with labels, the suggested secret, and model and region hints;
  - the model browser, through a shared `list_models()`.

| Provider | Adapter | Key secret | Notes |
|---|---|---|---|
| `openai` | OpenAI-compatible preset | `OPENAI_API_KEY` | `https://api.openai.com/v1`, `max_completion_tokens` |
| `anthropic` | **official `anthropic` SDK**, Messages API | `ANTHROPIC_API_KEY` | see below |
| `gemini` | native `generateContent` (httpx) | `GEMINI_API_KEY` | see below |
| `azure_openai` | OpenAI-compatible, v1 API | `AZURE_OPENAI_API_KEY` | `base_url` = resource endpoint (`/openai/v1` added); `api-key` header; model = deployment |
| `bedrock` | native Converse (httpx) | `AWS_BEARER_TOKEN_BEDROCK` | `base_url` = region; Bedrock API key as Bearer (no SigV4) |
| `mistral` | preset | `MISTRAL_API_KEY` | `https://api.mistral.ai/v1`, `max_tokens` |
| `groq` | preset | `GROQ_API_KEY` | `https://api.groq.com/openai/v1` |
| `deepseek` | preset | `DEEPSEEK_API_KEY` | `https://api.deepseek.com`, `max_tokens` |
| `together` | preset | `TOGETHER_API_KEY` | `https://api.together.ai/v1` |
| `xai` | preset | `XAI_API_KEY` | `https://api.x.ai/v1` |

- Native providers report token usage but no cost (they don't return one). Cost budgets therefore apply
  only where the provider reports cost (OpenRouter).

### Anthropic (via the official SDK, as the Claude API guidance requires for Python)
- **No silent server credentials:** an explicit key from the node's secret is required. The SDK would
  otherwise pick up credentials from the environment, so an unset secret is `PROVIDER_AUTH_MISSING`.
- **Request shape:**
  - top-level `system`; base64 `image` blocks; `tool_use` and `tool_result` blocks;
  - all of a turn's tool results in **one** user message;
  - automatic prompt caching (top-level `cache_control`).
- **No `temperature`:** the SDK speaks API version `2026-10-21`, where sampling parameters return 400, and
  current models reject them anyway.
- **Thinking room:** adaptive thinking shares `max_tokens`, so small caps are raised to 4096. Unused tokens
  are not billed.
- **Tool choice:** "required" maps to `any`, except on Claude Opus 5.5, Sonnet 5.5 and Fable / Mythos 5.1+,
  where forced tool choice returns 400. There it uses `auto`.
- **Thinking blocks are replayed:** the assistant content, thinking blocks included, is kept in the new
  `ChatResponse.provider_state` and sent back unchanged on the next request, as tool use requires. The Agent
  loop now records turns with `ChatResponse.assistant_message()`, so other providers can carry state too.
- **Refusals:** `stop_reason: refusal` raises `PROVIDER_REFUSED` with its category.
- **Fallbacks:** on models that support server-side fallbacks (Fable 5.1/5.2, Opus 5/5.5, Sonnet 5.5), the
  `fallbacks: "default"` beta is sent, so a declined request can be rerun on a fallback model. The answering
  model is reported in usage. `AGENTICMIND_ANTHROPIC_FALLBACKS=0` turns it off.

### Gemini
- **Request shape:**
  - `POST v1beta/models/{model}:generateContent` with `x-goog-api-key`;
  - roles `user` / `model`; `inlineData` images;
  - `functionDeclarations` with `parametersJsonSchema`, so plain JSON Schema needs no conversion;
  - `toolConfig.functionCallingConfig.mode` AUTO / ANY / NONE.
- **Ids and signatures:** function calls carry an `id`, which the `functionResponse` echoes. The model's
  parts are replayed unchanged through `provider_state`, so **thought signatures** survive multi-turn tool use
  (missing ones cause `MISSING_THOUGHT_SIGNATURE`).
- **Output tokens:** thinking shares `maxOutputTokens` (floor 4096). `thoughtsTokenCount` is counted as
  output.
- **Blocks:** blocked prompts and safety stops become `PROVIDER_REFUSED`.

### Bedrock
- **Request shape:**
  - Converse REST with `Authorization: Bearer <Bedrock API key>`;
  - `text`, `image` (format plus base64 `source.bytes`), `toolUse` and `toolResult` blocks (`json` or
    `text`, `status: error`);
  - `toolSpec.inputSchema.json`; tool choice `auto` / `any`.
- **Model ids:** model ids and inference-profile ids are URL-encoded.
- **Model list:** there is none (that needs the AWS control-plane API); enter an id.

### Upstream docs
- **Checked before building:**
  - the Claude API guidance (models, SDK, thinking, forced tool choice, refusals, fallbacks, caching);
  - Gemini's generateContent reference;
  - Azure's v1 lifecycle page;
  - Bedrock's Converse, ImageSource, ToolResultBlock, ToolChoice and API-key pages;
  - OpenAI, Mistral, Groq, DeepSeek and Together compatibility pages.
- **Standard usage, not confirmed on the pages read:** Gemini's `toolConfig` modes and auth header; xAI's
  chat-completions token field (it takes the common `max_tokens`).

## Verification
- **Request building and parsing:**
  - **Anthropic:**
    - image, tool and tool-result shapes with the merge and `is_error`;
    - the max-tokens floor and no temperature;
    - forced choice by model, thinking replay;
    - cached-token usage and refusals.
  - **Gemini:**
    - system instruction, schema, ANY mode;
    - function ids, merged results;
    - thought-signature replay, thought tokens, blocks.
  - **Bedrock:**
    - images, `toolUse` and `toolResult` with `status: error`;
    - tool choice, parsing, region endpoints.
- **HTTP round trips with mock transports:**
  - all six presets: URL, token field and auth header;
  - Azure: URL and `api-key`;
  - Gemini: endpoint, key header and model listing;
  - Bedrock: encoded model id and Bearer key.
  - **Anthropic through the real SDK:** an httpx2 mock transport checked the Bearer key, API version
    `2026-10-21`, no temperature, the fallbacks beta (and its opt-out), and that a missing key never falls
    back to server credentials.
- **Settings API:** labels, key secrets and the region hint.
- **Found by the tests:** the Anthropic SDK 2.x authenticates with `Authorization: Bearer` (not `x-api-key`)
  and sends API version `2026-10-21`.
- **Not live:** only an OpenRouter key is configured on this machine, so the native adapters are verified
  against documented shapes and mock transports, not live accounts.

## Consequences
- **Not covered:**
  - native embeddings for these providers (embeddings still go through the existing providers);
  - OpenAI's Responses API (Chat Completions is used);
  - Vertex AI (Google Cloud) as a separate provider.
- **Costs:** costs are unknown for native providers until a maintained price table is added.

## Revisit when
Native embeddings, the OpenAI Responses API, Vertex AI or Entra ID (keyless Azure) are needed, or price
tables for cost budgets.

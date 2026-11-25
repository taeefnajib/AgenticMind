# ADR-0041: Faster, sturdier agent runs

**Status:** Accepted.

## Context
A user's supervisor workflow (Supervisor on Grok 4.5, a Researcher with a Wikipedia Web Fetch tool, an Analyst
with a clock and calculator) took 72 s to answer "How many years since Bangladesh's independence?". Its
OpenTelemetry trace (ADR-0037) showed:
- **45 s wasted on retries.** Wikipedia refused every fetch (HTTP 403, bot policy). The Researcher reported
  the failure, and the Supervisor re-delegated the same question in new words four times, each a fresh
  ~10 s sub-agent run.
- **No control over thinking time.** Each turn of a reasoning model spends seconds on hidden thinking, and
  nodes offered no way to set its effort.
- **One dropped stream failed the run.** A follow-up run lost all its work when one OpenRouter stream dropped.
  The error message was empty. The official Python SDKs retry such calls; we did not.
- **Hidden token counts.** Per-turn token counts showed as `***` in traces: the redactor treated any key
  containing "token" as a credential.

## Decision
- **Permanent tool failures (`ToolError(permanent=True)`):**
  - The outermost Agent of a run owns a registry of tools that failed permanently, and its sub-agents share
    it. A later call to such a tool returns the reason at once, without running it. The step is marked
    `skipped`.
  - A sub-agent whose tools have all failed permanently is itself unavailable: the supervisor gets "cannot
    help in this run" instantly.
  - The sub-agent's result also carries `unavailable_tools`, plus a note not to delegate to it again.
  - Web Fetch marks bot-policy refusals as permanent. 404s and disallowed domains stay retryable, since the
    model can fix the URL.
  - The registry belongs to one agent run, so a new run starts clean.
- **`reasoning_effort` on LLM, Agent, Sub-agent and Reasoning nodes:**
  - Values: default | none | minimal | low | medium | high | xhigh | max. `default` sends nothing.
  - Mapped to each provider's documented control (checked against current docs):
    - OpenRouter: `reasoning.effort` (`none` disables reasoning);
    - OpenAI and Azure OpenAI: `reasoning_effort`;
    - Anthropic: `output_config.effort` (low…max; thinking cannot be turned off on current models, so
      none/minimal → low);
    - Gemini 3+: `generationConfig.thinkingConfig.thinkingLevel` (MINIMAL…XHIGH; there is no "off").
  - Providers without a documented control don't receive it.
- **Retries:** `chat_stream` retries transient provider errors (429, 5xx, dropped connections) up to 2 times
  with jittered backoff (0.5 s, 1.5 s).
  - It retries only before any text reached the live preview, so text never appears twice.
  - It skips Anthropic, whose SDK already retries.
  - Planner/composer and Reasoning calls now go through it too.
  - Transport errors name the exception type instead of an empty message.
- **Redaction:** usage counts (`input_tokens`, `max_tokens`, `token_budget`, …) are no longer hidden;
  `access_token`, `refresh_token` and `access_tokens` still are.

## Verification
- **Unit tests:**
  - the field run replayed with scripted models: one fetch in the whole run, the cause reaches the
    supervisor, later delegations answered in under 50 ms, a new run starts clean;
  - effort mapping per provider, and nothing sent where there is no control;
  - nodes pass effort only when set;
  - retries: two then success, the non-streamed path, out of retries, non-transient errors, text already
    shown, Anthropic not double-retried;
  - counts visible while credentials stay hidden.
- **Live** (copies of the user's workflow on a test server, same question):
  - with the fixes at default effort: 30–43 s over three runs (was 72 s), one Researcher attempt (was four),
    correct answer "55 years";
  - one earlier run failed on a dropped stream; that failure is what led to the retries.
- **Low effort is not free:**
  - at `low` on every agent, the run took 13.8 s but answered "54 years (as of 2025)". The Supervisor skipped
    the Analyst and its clock, and guessed the year from training data;
  - in another run, the Analyst at `low` made three separate calculator calls.
  - Effort is therefore a per-node choice, not a global default.

## Consequences
- **Remaining time is model turns.** At default effort, 8 turns of 3–5 s each make up most of the time. Each
  delegation costs at least two sub-agent turns plus a supervisor turn.
- **Wikipedia:** Web Fetch still needs `AGENTICMIND_HTTP_USER_AGENT` with real contact details.
  Wikimedia refuses user agents without a contact URL; we checked this live.

## Revisit when
Adding the current date to model prompts, per-route model choice inside agents, or caching tool results
across runs.

# ADR-0037: Observability export: OpenTelemetry traces and Prometheus metrics

**Status:** Accepted. Feature 5 of the open-source production plan (ADR-0032).

## Context
Runs are already recorded as first-class traces (ADR-0005 and later) and shown in the IDE. Production teams
want those traces in the tools they already run: OpenTelemetry backends such as Jaeger, Grafana Tempo,
Honeycomb and Datadog, LLM-observability tools such as Langfuse and Arize Phoenix, and Prometheus / Grafana
for metrics.

## Decision

### `agenticmind-observability` package
- **Trace conversion:** a finished run's persisted trace events become one OpenTelemetry trace after the run
  (`run_to_spans`), not through live instrumentation. This makes it:
  - exact (built from the recorded trace);
  - re-exportable (ids are derived from the run id);
  - zero-cost while running.
- **Span tree:**
  - a root `invoke_workflow <project>` span with run id, revision, status, total tokens and cost;
  - one span per node invocation (loop iterations and retries are separate);
  - model-using nodes as GenAI spans: `chat <model>`, or `invoke_agent <label>` for Agent / Reasoning /
    Tool Router, with `gen_ai.operation.name`, `gen_ai.provider.name` (well-known values, e.g. `aws.bedrock`,
    `gcp.gen_ai`), `gen_ai.request.model` / `response.model`, `gen_ai.usage.input_tokens` / `output_tokens`,
    `gen_ai.usage.cost` and `gen_ai.agent.name`;
  - agent tool calls as `execute_tool <tool>` child spans (`gen_ai.tool.name`), timed from their recorded
    duration;
  - the other node events (agent turns, retries, guardrail blocks, ...) as span events;
  - failures carry status ERROR and `error.type`.
- **Content is opt-in** (`include_content`): input and output summaries and tool arguments and results can
  contain personal data. When enabled, they are added under the GenAI tool attributes and under the
  Langfuse and OpenInference keys those tools display (`langfuse.observation.input/output`,
  `input.value` / `output.value`).
- **Encodings:** OTLP/HTTP **protobuf** (default, via `opentelemetry-proto`) or **JSON** (hex trace and span
  ids, 64-bit integers as strings, enums as integers, per the OTLP spec). The span model is neutral, so
  protobuf's own JSON mapping (base64 ids) is not used.
- **Transport:** retries 429/502/503/504 and network errors with backoff; 400 is not retried.
- **Metrics:** `RunMetrics` keeps Prometheus counters from finished runs:
  - runs by status;
  - node failures by type and code;
  - model calls, tokens (by provider, model and direction) and cost;
  - tool calls by tool and outcome;
  - a run-duration histogram, plus an active-runs gauge.

### Configuration (standard OpenTelemetry variables)
- **Variables:**
  - `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is used as-is, or `OTEL_EXPORTER_OTLP_ENDPOINT` with `/v1/traces`
    appended;
  - `OTEL_EXPORTER_OTLP_[TRACES_]HEADERS` (`k=v,k=v`, percent-decoded);
  - `OTEL_EXPORTER_OTLP_[TRACES_]PROTOCOL` (`http/protobuf` or `http/json`; gRPC is refused with a clear
    error);
  - `OTEL_SERVICE_NAME`, and `AGENTICMIND_OTEL_INCLUDE_CONTENT`.
- **Settings → Observability (admins) overrides them.** Fields:
  - endpoint and protocol, service name and content opt-in;
  - a **headers secret**: header values such as API keys stay in the secret vault, never in settings, and
    the page shows header names only;
  - presets for an OTel Collector / Jaeger / Tempo, Langfuse Cloud (with its `Authorization=Basic …` plus
    `x-langfuse-ingestion-version=4` header format) and local Phoenix;
  - a "Send test span" button, and export status (counts, last error).

### API
- **Automatic export:** `RunManager.finish_hooks` call the observability service after every finished run.
  It updates the metrics and exports in a background task, which never slows or fails a run.
- **`GET /api/metrics`:** Prometheus text format. With `AGENTICMIND_METRICS_TOKEN` set, scrapers send
  `Authorization: Bearer <token>` (error code `METRICS_TOKEN_REQUIRED`). Otherwise the usual `/api` rules
  apply.
- **Run endpoints:** `GET /api/runs/{id}/otlp` downloads a run as OTLP/JSON (also linked from the IDE's Trace
  tab), and `POST /api/runs/{id}/export` re-exports a run.

### Upstream docs
- **Checked:**
  - the OTLP spec (paths, ports, content types, JSON rules, retryable statuses) and exporter configuration
    (env vars, endpoint rules, headers format);
  - the GenAI attribute registry (now maintained in a separate repository);
  - Langfuse's OpenTelemetry page (endpoint, Basic auth, ingestion-version header, both encodings,
    attribute mapping).
- **Not confirmed:** Phoenix's exact OTLP path. The `/v1/traces` default is used.

## Verification
- **From a real engine run** (a mock LLM, plus an Agent calling the calculator):
  - span tree and parents, GenAI attributes;
  - `invoke_agent` vs `chat`, the tool span under its agent with sane timing;
  - agent turns as span events, deterministic ids;
  - content excluded by default and included on request.
- **Encodings:** JSON (hex ids, string nanos, int enums), and protobuf decoded back with
  `ExportTraceServiceRequest`.
- **Configuration:** env rules (base vs signal endpoint, percent-decoded Langfuse headers, gRPC refused).
- **Transport:** a retry after 503, no retry on 400, both content types.
- **Metrics:** text format.
- **API:** metrics with the token, a trace download, settings validation, a header secret resolved but
  never shown, a test span, automatic export after a run, manual re-export, disabling, and reset.
- **Live:** an API configured only through `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` ran a
  Gemini agent with the calculator ("17.5% of 2480 is 434"). A local OTLP receiver decoded one protobuf
  request at `/v1/traces` with the auth header:
  - six spans in one trace: workflow (5.8 s), `invoke_agent Analyst` (model, 340/105 tokens, $0.000649),
    `execute_tool calculator`, and the other nodes;
  - `/api/metrics` showed the matching counters.
- **Not checked:** the new Settings section in the browser (the extension disconnected). It is covered by
  the type check and API tests.

## Consequences
- **Export timing:** traces arrive once a run ends, not during it. A waiting run (human approval) is exported
  when it finishes.
- **Metrics:** counters reset when the API restarts, which Prometheus handles.
- **Not covered:** CLI runs and exported apps don't export yet; log records and OTLP metrics push are not
  implemented.

## Revisit when
Live spans for long runs, OTLP logs and metrics, export from the CLI and exported apps, or sampling are
needed.

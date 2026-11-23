# ADR-0035: Triggers: webhooks, schedules, queues and chat channels

**Status:** Accepted. Feature 3 of the open-source production plan (ADR-0032).

## Context
Graphs could only start from the Run button, the API, a deployed endpoint or MCP. Production agents start
from events: HTTP callbacks, timetables, queue messages and chat messages. They also answer back in the same
channel.

## Decision

### Triggers are graph nodes
- **The nodes:**
  - `webhook_trigger`;
  - `schedule_trigger`;
  - `queue_trigger` (Redis Streams or Amazon SQS);
  - `chat_trigger` (Slack, Telegram, WhatsApp Cloud API, email over IMAP/SMTP).
  - All are in *Inputs & triggers*, and each outputs `payload`, `text` and `event`.
- **Versioned with the graph:** they are visible on the canvas and exported like any node. No IR change was
  needed.
- **Activation:** a trigger listens only when `enabled` is ticked in the project's **latest saved revision**.
- **Manual runs:** in the IDE, CLI or tests, a trigger node outputs its `sample` event, marked
  `simulated: true`, so trigger graphs stay testable everywhere. Real events arrive in the run input under
  `_trigger`, keyed by node id.

### The trigger service (API process)
- **Activation and refresh:**
  - Scans every project's latest revision at start-up, after each create, save or delete (a thread-safe
    wake-up), and every 60 s.
  - Unchanged triggers keep their counters, schedule and poller.
- **Readiness:** a trigger missing settings or secrets is listed as *not ready* with what it needs. Its
  endpoint answers 503 rather than failing at run time.
- **Each event:**
  - starts an ordinary recorded run of that revision, with the event in the run's inputs (visible in run
    history);
  - is deduplicated by event id (Slack retries, WhatsApp redeliveries, Telegram `update_id`).
- **Chat replies:** chat triggers send the configured run output (`reply_output`, default `answer`) back to
  the conversation when the run ends. A failed run sends `error_reply`. Delivery problems appear as the
  trigger's `last_error`.
- **Status:** `GET /api/projects/{id}/triggers` reports active or not ready, the path, next run, event count,
  last event and run, and last error. The IDE shows it in a **Trigger** panel with per-channel setup steps
  (Telegram `setWebhook` URL, Slack Request URL, Meta callback URL), plus a note that localhost needs a tunnel.

### Endpoints and verification
- `POST/GET /api/triggers/{project}/{node}` is **public**: there's no session, and each trigger
  authenticates its caller:
  - **Webhook:**
    - `token`: `Authorization: Bearer` or `X-Webhook-Token`;
    - `hmac`: HMAC-SHA256 of the raw body, `sha256=<hex>` in a configurable header (GitHub style);
    - `none`: explicit opt-in only.
    - Responses are `ack` (202 and the run id) or `wait` (the outputs, or a single output, after up to
      `wait_seconds`).
  - **Slack:**
    - checks the `v0=` HMAC of `v0:{ts}:{body}` with the signing secret and a 5-minute replay window, verified
      against Slack's documented example vector;
    - answers `url_verification`;
    - ignores bot messages and subtypes, acknowledges at once (Slack's 3-second rule), and replies with
      `chat.postMessage` (in thread by default).
  - **Telegram:** checks the `X-Telegram-Bot-Api-Secret-Token` header (set with `setWebhook`), skips bots, and
    replies with `sendMessage` + `reply_parameters`. Errors never echo the bot-token URL.
  - **WhatsApp:**
    - the GET `hub.verify_token` handshake;
    - `X-Hub-Signature-256` HMAC of the body with the app secret;
    - replies via `graph.facebook.com/v27.0/{phone_number_id}/messages` (the version is configurable), with
      `context.message_id`.
- **Limits:** bodies over 2 MB are refused. Secrets are compared in constant time and resolved per request,
  never stored on the trigger.

### Schedules, queues, mailboxes
- **Schedules:**
  - cron with a time zone, or an interval;
  - a small built-in cron parser: lists, ranges, steps, names, `@daily`-style aliases, and the standard rule
    that either restricted day field may match;
  - checked every 5 s.
  - Missed runs are not replayed after a restart.
- **Redis Streams:** a consumer group (created if missing). A single JSON field (`data`/`payload`/`body`/
  `json`) is parsed. **SQS:** long polling. Both acknowledge `on_start` or `on_success`; `on_success` leaves a
  failed run's message for redelivery.
- **Email:** IMAP polling of unread mail, marked seen once read. Replies go by SMTP with `Re:`,
  `In-Reply-To` and `References`.

### Upstream docs
Verified against Slack's Events API and request-signing pages, the Telegram Bot API (setWebhook, Update,
Message, ReplyParameters), and Meta's WhatsApp webhook and text-message pages, plus Graph versioning (v27.0
is the latest). Three forms come from standard usage rather than the pages read: Slack's `url_verification`
echo, the `Bearer` header for `chat.postMessage`, and WhatsApp's `context.message_id` reply field.

### Template
**Chat bot (Telegram, Slack, WhatsApp, email)**: a disabled Chat Trigger feeding an LLM whose `answer` output
is the reply.

## Verification
- **Unit tests:**
  - cron: steps, weekday ranges, names, leap day, aliases, the either-day rule, New York time, and rejected
    expressions;
  - trigger nodes: the real event vs. the sample vs. another node's event.
- **End to end in the API** (with the service running, real runs, a mock outbound transport, and fake IMAP,
  SMTP, Redis and SQS):
  - webhook token and wait response, HMAC and ack;
  - disabled (404) and unconfigured (503, with the missing secret named);
  - Slack challenge, wrong signature, a threaded reply with the bot token, the retry deduplicated and the bot
    message ignored;
  - Telegram header and reply payload; WhatsApp handshake, signature and reply payload;
  - a schedule firing once when due;
  - email poll, mark seen and SMTP reply headers;
  - Redis ack after success, SQS delete.
- **Live (OpenRouter / Gemini, real server):**
  - the webhook refused a call without a token, then answered "The capital of Australia is Canberra." in
    `wait` mode;
  - a `* * * * *` schedule ran on the minute (13:15 UTC);
  - the IDE's Trigger panel showed *Listening*, the next run and the event count.
- **Not live:** real Slack, Telegram and WhatsApp deliveries need your app credentials and a public URL.

## Consequences
- **One API process:** triggers run inside the API process. Several API processes on one database would
  each fire schedules and poll queues (redelivery is deduplicated per process only). Durable execution
  (feature 7) moves this to workers with locking.
- **Public URL needed:** chat platforms must reach the server over the internet (a tunnel or a public host
  with HTTPS).
- **Exports:** exported standalone apps (ADR-0019) do not host triggers yet.

## Revisit when
Multi-process deployments, a catch-up policy for missed schedules, more channels (Discord, Teams), Kafka /
RabbitMQ, or triggers in exported apps are needed.

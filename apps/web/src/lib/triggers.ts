/** Trigger nodes (ADR-0035): which node types are triggers, and how to connect each one. */

export interface TriggerStatus {
  node_id: string;
  kind: "webhook" | "schedule" | "queue" | "chat";
  revision: number;
  active: boolean;
  problem: string | null;
  events: number;
  last_event_at: string | null;
  last_run_id: string | null;
  last_error: string | null;
  next_run_at: string | null;
  path?: string;
  channel?: "slack" | "telegram" | "whatsapp" | "email";
}

export const TRIGGER_TYPES = new Set([
  "agenticmind.core.webhook_trigger",
  "agenticmind.core.schedule_trigger",
  "agenticmind.core.queue_trigger",
  "agenticmind.core.chat_trigger",
]);

export const isTriggerType = (type: string) => TRIGGER_TYPES.has(type);

/** One-paragraph setup instructions for a trigger, given its public URL. */
export function setupHint(status: TriggerStatus, url: string | null): string {
  const where = url ?? "(this trigger's URL)";
  if (status.kind === "webhook") {
    return `POST JSON to ${where}. With token auth send "Authorization: Bearer <secret>"; with HMAC send the body's HMAC-SHA256 as "sha256=<hex>".`;
  }
  if (status.kind === "schedule") return "Runs automatically while the server is running.";
  if (status.kind === "queue") return "Each message starts one run; the payload is the message body (JSON is parsed).";
  switch (status.channel) {
    case "slack":
      return `In your Slack app: Event Subscriptions → Request URL ${where}; subscribe to message.channels / message.im or app_mention; scope chat:write for replies.`;
    case "telegram":
      return `Register the webhook once: https://api.telegram.org/bot<token>/setWebhook?url=${encodeURIComponent(where)}&secret_token=<your secret token>`;
    case "whatsapp":
      return `In Meta's app dashboard: WhatsApp → Configuration → Callback URL ${where}, Verify token = your verify token; subscribe to the "messages" field.`;
    case "email":
      return "Polls the mailbox for unread messages; each one starts a run and the reply is sent by SMTP.";
    default:
      return "";
  }
}

/** Absolute URL for a trigger path; webhooks need a public HTTPS address in production (e.g. a tunnel). */
export function triggerUrl(origin: string, path: string | undefined): string | null {
  return path ? `${origin.replace(/\/$/, "")}${path}` : null;
}

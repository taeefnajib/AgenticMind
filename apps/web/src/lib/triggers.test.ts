import { describe, expect, it } from "vitest";

import { isTriggerType, setupHint, triggerUrl, type TriggerStatus } from "./triggers";

const base: TriggerStatus = {
  node_id: "t", kind: "chat", revision: 1, active: true, problem: null, events: 0, last_event_at: null,
  last_run_id: null, last_error: null, next_run_at: null, path: "/api/triggers/p1/t", channel: "telegram",
};

describe("triggers", () => {
  it("recognises trigger node types", () => {
    expect(isTriggerType("agenticmind.core.chat_trigger")).toBe(true);
    expect(isTriggerType("agenticmind.core.manual_input")).toBe(false);
  });

  it("builds URLs and per-channel setup hints", () => {
    const url = triggerUrl("https://bots.example.com/", base.path);
    expect(url).toBe("https://bots.example.com/api/triggers/p1/t");
    expect(setupHint(base, url)).toContain("setWebhook?url=https%3A%2F%2Fbots.example.com%2Fapi%2Ftriggers%2Fp1%2Ft");
    expect(setupHint({ ...base, channel: "slack" }, url)).toContain("Request URL https://bots.example.com/api/triggers/p1/t");
    expect(setupHint({ ...base, kind: "schedule", path: undefined }, null)).toContain("automatically");
    expect(triggerUrl("x", undefined)).toBeNull();
  });
});

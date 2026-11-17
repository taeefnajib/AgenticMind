import { describe, expect, it } from "vitest";

import type { RunOut, TraceEvent } from "../api/types";
import { toMessage } from "./ChatPanel";
import { compareVersions } from "./NodeLibrary";

const run = (over: Partial<RunOut>): RunOut => ({
  id: "run_1", project_id: "p", revision: 1, status: "succeeded", inputs: {}, outputs: {}, error: null,
  node_status: {}, content_hash: "h", created_at: null, started_at: null, finished_at: null, ...over,
});
const ev = (over: Partial<TraceEvent>): TraceEvent => ({
  run_id: "run_1", project_id: "p", seq: 1, timestamp: "2026-01-01T00:00:00Z", event_type: "node_succeeded", ...over,
});

describe("chat messages", () => {
  it("renders answer objects with citations, tokens and cost", () => {
    const events = [
      ev({ usage: { provider: "openrouter", model: "g", input_tokens: 900, output_tokens: 60, estimated_cost_usd: 0.0012 } }),
      ev({ seq: 2, event_type: "run_succeeded", duration_ms: 2300 }),
    ];
    const outputs = { answer: { answer: "25 days [1].", citations: [{ n: 1, filename: "h.md" }], validation: { invalid: [7] } } };
    const m = toMessage({ run: run({ outputs }), events }, "answer");
    expect(m.text).toBe("25 days [1].");
    expect(m.citations).toEqual([{ n: 1, filename: "h.md" }]);
    expect(m.invalid).toEqual([7]);
    expect(m.meta).toEqual({ ms: 2300, tokens: 960, cost: 0.0012 });
  });

  it("renders agent results with tool steps", () => {
    const outputs = {
      answer: {
        answer: "75 days in total.",
        steps: [{ tool: "calculator", arguments: { expression: "25*3" }, ok: true, result_summary: "75" }],
        report: { stop_reason: "final_answer" },
      },
    };
    const m = toMessage({ run: run({ outputs }), events: [] }, "answer");
    expect(m.text).toBe("75 days in total.");
    expect(m.steps?.[0]?.tool).toBe("calculator");
    expect(m.stopReason).toBe("final_answer");
    expect(m.citations).toBeUndefined();
  });

  it("shows failures and plain text outputs", () => {
    const failed = toMessage(
      { run: run({ status: "failed", error: { code: "PROVIDER_AUTH_FAILED", message: "bad key" } }), events: [] },
      "answer",
    );
    expect(failed.error).toBe("PROVIDER_AUTH_FAILED: bad key");
    expect(toMessage({ run: run({ outputs: { answer: "hello" } }), events: [] }, "answer").text).toBe("hello");
    expect(toMessage({ run: run({ outputs: {} }), events: [] }, "answer").error).toMatch(/no 'answer' output/);
  });
});

describe("node library versions", () => {
  it("compares semantic versions", () => {
    expect(compareVersions("1.1.0", "1.0.0")).toBeGreaterThan(0);
    expect(compareVersions("1.10.0", "1.9.9")).toBeGreaterThan(0);
    expect(compareVersions("2.0.0-beta", "2.0.0")).toBeLessThan(0);
    expect(compareVersions("1.0.0", "1.0.0")).toBe(0);
  });
});

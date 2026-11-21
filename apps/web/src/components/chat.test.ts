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

describe("chat memory inputs", () => {
  it("adds conversation id and consent only when the graph reads them", async () => {
    const { chatInputs, newConversationId } = await import("./ChatPanel");
    const node = (key: string, value_type = "String") => ({
      id: key, type: "agenticmind.core.manual_input", version: "1.0.0", config: { key, value_type },
    });
    expect(chatInputs([node("question")], "question", "hi", "c1", true)).toEqual({ question: "hi" });
    const graph = [node("question"), node("conversation_id"), node("memory_consent", "Boolean")];
    expect(chatInputs(graph, "question", "hi", "c1", false)).toEqual({
      question: "hi", conversation_id: "c1", memory_consent: false,
    });
    expect(newConversationId()).toMatch(/^chat-[a-z0-9]+-[a-z0-9]+$/);
    expect(newConversationId()).not.toEqual(newConversationId());
  });
});

describe("approval responses", () => {
  it("builds responses and enforces comments / JSON edits", async () => {
    const { buildResponse } = await import("./ApprovalsPanel");
    const wait = (value: unknown, extra: Record<string, unknown> = {}) => ({
      id: 1, run_id: "r", project_id: "p", node_id: "review", node_type: "t", status: "pending" as const,
      response_schema: null, response: null, created_at: null, answered_at: null,
      request: { value, allow_edit: true, require_comment_on_reject: true, ...extra } as never,
    });
    const form = { comment: "", reviewer: "", edited: "hello" };
    expect(buildResponse(wait("hello"), "approved", form)).toEqual({ decision: "approved" });
    expect(buildResponse(wait("hello"), "approved", { ...form, edited: "hi!" })).toEqual({ decision: "approved", value: "hi!" });
    expect(buildResponse(wait("hello"), "rejected", form)).toBe("Please explain why you are rejecting.");
    expect(buildResponse(wait("hello"), "rejected", { ...form, comment: " no " })).toEqual({ decision: "rejected", comment: "no" });
    expect(buildResponse(wait({ a: 1 }), "approved", { ...form, edited: "{bad" })).toBe("The edited value is not valid JSON.");
    expect(buildResponse(wait({ a: 1 }), "approved", { ...form, edited: '{"a": 2}' })).toEqual({ decision: "approved", value: { a: 2 } });
  });
});

describe("NL-to-SQL answers", () => {
  it("carry the SQL and rows into the chat message", async () => {
    const { toMessage } = await import("./ChatPanel");
    const run = { id: "r", status: "succeeded", outputs: { answer: { answer: "Revenue by category. (2 rows)", sql: "SELECT 1", columns: ["category", "revenue"], rows: [{ category: "Office", revenue: 10 }, { category: "Bags", revenue: 5 }], row_count: 2, truncated: false } } } as never;
    const m = toMessage({ run, events: [] }, "answer");
    expect(m.text).toBe("Revenue by category. (2 rows)");
    expect(m.sql).toEqual({ query: "SELECT 1", columns: ["category", "revenue"], rows: [{ category: "Office", revenue: 10 }, { category: "Bags", revenue: 5 }], total: 2, truncated: false });
  });
});

describe("agent step shapes (ADR-0029)", () => {
  it("shows plan-and-execute steps as steps with nested tool calls", async () => {
    const { normaliseSteps } = await import("./ChatPanel");
    const [plan, react] = normaliseSteps([
      { plan_step: 1, step: "Compute 12*3", result: "36", ok: true, tools: [{ tool: "calculator", ok: true }] },
      { tool: "calculator", ok: true, thought: "multiply" },
    ]);
    expect(plan).toMatchObject({ tool: "plan step 1: Compute 12*3", result_summary: "36", ok: true });
    expect(plan?.substeps?.[0]?.tool).toBe("calculator");
    expect(react?.thought).toBe("multiply");
  });
});

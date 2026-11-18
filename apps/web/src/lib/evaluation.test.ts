import { describe, expect, it } from "vitest";

import { cleanCase, formatDelta, formatMetric, newCase, parseSource, sourceLabel, splitLines } from "./evaluation";

describe("evaluation helpers", () => {
  it("parses expected sources like the server", () => {
    expect(parseSource("handbook.md#Annual leave")).toEqual({ filename: "handbook.md", section: "Annual leave" });
    expect(parseSource("handbook.md")).toEqual({ filename: "handbook.md" });
    expect(parseSource("art_0123456789abcdef#c2")).toEqual({ id: "art_0123456789abcdef#c2" });
    expect(parseSource("chunk-7")).toEqual({ id: "chunk-7" });
    expect(parseSource("  ")).toBeNull();
    expect(sourceLabel({ filename: "a.md", section: "Intro" })).toBe("a.md#Intro");
    expect(splitLines("a\n\n b ; c")).toEqual(["a", "b", "c"]);
  });

  it("formats metrics by kind", () => {
    expect(formatMetric(0.8333, "recall_at_k")).toBe("83%");
    expect(formatMetric(1520, "latency_ms", "ms")).toBe("1.5 s");
    expect(formatMetric(0.0021, "cost_usd", "USD")).toBe("$0.00210");
    expect(formatMetric(null, "mrr")).toBe("—");
    expect(formatDelta(-0.25, "mrr")).toBe("−25 pts");
    expect(formatDelta(120, "latency_ms", "ms")).toBe("+120 ms");
  });

  it("creates and cleans cases", () => {
    const c = newCase([{ id: "case_1", inputs: {} }], ["question"]);
    expect(c).toEqual({ id: "case_2", inputs: { question: "" } });
    expect(cleanCase({ ...c, reference: "  ", must_include: [], expected_sources: [{ filename: "a.md" }] })).toEqual({
      id: "case_2",
      inputs: { question: "" },
      expected_sources: [{ filename: "a.md" }],
    });
  });
});

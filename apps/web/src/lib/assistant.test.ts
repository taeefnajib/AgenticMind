import { describe, expect, it } from "vitest";

import type { Diagnostic } from "../api/types";
import { describeChanges, shortType, splitDiagnostics } from "./assistant";

describe("assistant helpers", () => {
  it("summarises changes", () => {
    expect(describeChanges({})).toBe("No changes to the graph");
    expect(
      describeChanges({
        added: [{ id: "a", type: "t" }, { id: "b", type: "t" }],
        modified: [{ id: "c", type: "t" }],
        edges_added: 1,
        edges_removed: 2,
      }),
    ).toBe("2 nodes added · 1 changed · 1 connection added · 2 connections removed");
  });

  it("separates environment notes from blocking errors", () => {
    const d = (code: string, severity: Diagnostic["severity"] = "error"): Diagnostic => ({ code, severity, message: code });
    const { blocking, notes } = splitDiagnostics([d("SECRET_NOT_CONFIGURED"), d("EDGE_TYPE_MISMATCH"), d("X", "warning"), d("Y", "info")]);
    expect(blocking.map((x) => x.code)).toEqual(["EDGE_TYPE_MISMATCH"]);
    expect(notes.map((x) => x.code)).toEqual(["SECRET_NOT_CONFIGURED", "X"]);
  });

  it("shortens core node types only", () => {
    expect(shortType("agenticmind.models.llm")).toBe("llm");
    expect(shortType("acme.text.word_stats")).toBe("acme.text.word_stats");
  });
});

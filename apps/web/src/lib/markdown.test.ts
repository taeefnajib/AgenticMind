import { describe, expect, it } from "vitest";

import { parseBlocks, renderInline } from "./markdown";

describe("markdown", () => {
  it("groups paragraphs, bullet and numbered lists", () => {
    const blocks = parseBlocks("Intro line\nsecond line\n\n* one\n- two\n\n1. first\n2) second\n## Head");
    expect(blocks).toEqual([
      { kind: "p", lines: ["Intro line", "second line"] },
      { kind: "ul", items: ["one", "two"] },
      { kind: "ol", items: ["first", "second"] },
      { kind: "h", text: "Head" },
    ]);
  });

  it("renders inline styles and citations without HTML injection", () => {
    const parts = renderInline("**75 days** and *720 EUR* `x` [2] <script>alert(1)</script>", (n) => `CITE${n}`);
    const types = parts.map((p) => (typeof p === "string" ? "text" : (p as { type: string }).type));
    expect(types).toEqual(["strong", "text", "em", "text", "code", "text", "text", "text"]);
    expect(parts).toContain("CITE2");
    expect(parts[parts.length - 1]).toBe(" <script>alert(1)</script>"); // stays plain text
  });
});

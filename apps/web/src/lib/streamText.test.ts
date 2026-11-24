import { describe, expect, it } from "vitest";

import { applyToken, EMPTY_LIVE, latestText } from "./streamText";

describe("live streamed text", () => {
  it("accumulates per node, follows the latest node and ignores replays", () => {
    let s = applyToken(EMPTY_LIVE, { seq: 1, node_id: "llm", invocation_id: "i", text: "Hel" });
    s = applyToken(s, { seq: 2, node_id: "llm", invocation_id: "i", text: "lo" });
    expect(latestText(s)).toBe("Hello");
    s = applyToken(s, { seq: 2, node_id: "llm", invocation_id: "i", text: "lo" }); // reconnect replay
    expect(latestText(s)).toBe("Hello");
    s = applyToken(s, { seq: 3, node_id: "agent", invocation_id: "j", text: "Hi" });
    expect(latestText(s)).toBe("Hi");
    expect(s.byNode.llm).toBe("Hello");
    expect(latestText(EMPTY_LIVE)).toBe("");
  });
});

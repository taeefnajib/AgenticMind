import { describe, expect, it } from "vitest";

import { mcpEndpoint, mcpToolName, readExposure, writeExposure } from "./mcp";

describe("MCP exposure", () => {
  it("names tools like the backend", () => {
    expect(mcpToolName("Greeter Bot!")).toBe("greeter_bot");
    expect(mcpToolName("  ")).toBe("graph");
    expect(mcpToolName("a".repeat(80))).toHaveLength(64);
  });

  it("reads and writes metadata.mcp without touching other metadata", () => {
    expect(readExposure({})).toEqual({ expose: false });
    expect(readExposure({ mcp: { expose: true, tool_name: "hi" } })).toEqual({ expose: true, tool_name: "hi" });
    expect(readExposure({ mcp: "nonsense" })).toEqual({ expose: false });
    const meta = { other: 1 };
    expect(writeExposure(meta, { expose: true })).toEqual({ other: 1, mcp: { expose: true } });
    expect(writeExposure({ ...meta, mcp: { expose: true } }, { expose: false })).toEqual({ other: 1 });
    expect(writeExposure(meta, { expose: false, tool_name: " keep " })).toEqual({ other: 1, mcp: { expose: false, tool_name: "keep" } });
  });

  it("builds the endpoint URL", () => {
    expect(mcpEndpoint("http://localhost:5173/")).toBe("http://localhost:5173/api/mcp");
  });
});

import type { JsonValue } from "../api/types";

/** Project-level MCP exposure, stored in the IR as ``metadata.mcp`` (ADR-0033). */
export interface McpExposure {
  expose: boolean;
  tool_name?: string;
}

/** Mirrors ``mcp_tool_name`` in agenticmind_runtime.mcp_server: the name MCP clients see. */
export function mcpToolName(raw: string): string {
  const name = raw
    .trim()
    .toLowerCase()
    .replace(/[^A-Za-z0-9_-]+/g, "_")
    .replace(/^[_-]+|[_-]+$/g, "");
  return (name || "graph").slice(0, 64);
}

export function readExposure(metadata: Record<string, JsonValue> | undefined): McpExposure {
  const raw = metadata?.mcp;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const toolName = typeof raw.tool_name === "string" ? raw.tool_name : undefined;
    return { expose: raw.expose === true, ...(toolName ? { tool_name: toolName } : {}) };
  }
  return { expose: false };
}

/** Metadata with the exposure applied; ``mcp`` is removed entirely when not exposed and unnamed. */
export function writeExposure(metadata: Record<string, JsonValue> | undefined, mcp: McpExposure): Record<string, JsonValue> {
  const next: Record<string, JsonValue> = { ...(metadata ?? {}) };
  const toolName = mcp.tool_name?.trim();
  if (!mcp.expose && !toolName) delete next.mcp;
  else next.mcp = { expose: mcp.expose, ...(toolName ? { tool_name: toolName } : {}) };
  return next;
}

/** MCP endpoint of this AgenticMind server, as an MCP client should be configured. */
export function mcpEndpoint(origin: string): string {
  return `${origin.replace(/\/$/, "")}/api/mcp`;
}

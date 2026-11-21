# ADR-0033: MCP client and server

**Status:** Accepted. Feature 1 of the open-source production plan (ADR-0032).

## Context
The Model Context Protocol (MCP) is how agents reach external tools (GitHub, Slack, databases, file systems,
internal APIs) and how AI applications call other agents. Without it every integration needed a custom node.
We checked the upstream SDK before building:
- `mcp` 2.3.0 (October 2026) speaks protocol version 2026-07-28 and the earlier handshake versions.
- The v2 API is `mcp.Client`, `streamable_http_client` over httpx2, and the low-level `mcp.server.Server`
  with `on_list_tools` / `on_call_tool`.
- SSE is superseded, so it is not offered.

## Decision

### Client: MCP Server node (`agenticmind.agents.mcp_tools@1.0.0`)
- **Connection:** a local command over **stdio** (`command`, `args`, `env`, `secret_env`, `cwd`), or a
  **Streamable HTTP** URL (`bearer_token` secret, `headers`).
- **Output:** one *toolkit* tool value (`tool_type: "mcp"`), holding the connection and the listed tools, plus
  a `catalog`. Options: `name_prefix`, `include_tools`, `exclude_tools`.
- **Tool names** are made safe: `word.count` becomes `word_count`, and a duplicate after cleaning is an
  error.
- **Toolkit expansion:** Agent, Tool Router, Tool Chain and Subagent expand the toolkit into one tool per MCP
  tool (`load_tools`). Each call opens a connection and closes it afterwards: simple and leak-free, but a
  stdio server starts once per call.
- **Results:**
  - structured content is used as the result. A plain value the SDK wrapped as `{"result": v}` is unwrapped
    when the text says the same thing; otherwise single-text JSON is parsed;
  - images and resources are summarised;
  - `isError` becomes a tool error the model sees.
- **Secrets:** the MCP node's secret references flow to the Agent the usual way (transitively along tool
  edges), and are resolved only when a call runs.
- **stdio environment:** the child inherits only the SDK's safe allow-list (PATH, home and temp locations)
  plus the configured variables, so the AgenticMind server's own keys never reach MCP servers.
- **HTTP targets:** localhost is allowed (local MCP servers are normal). Link-local and metadata addresses are
  refused.

### Client: MCP Call node (`agenticmind.agents.mcp_call@1.0.0`)
- One tool call with default arguments, merged with the `arguments` input.
- Outputs `result`, `text` and `is_error`. `fail_on_tool_error` chooses between failing the node and
  reporting the error.

### Permissions and node versions
- stdio tools need the `subprocess` permission, so new versions declare it:
  - Agent **1.2.0**;
  - Tool Router **1.1.0**;
  - Tool Chain **1.1.0**.
- Earlier versions keep their exact permissions and refuse stdio MCP tools with `TOOL_PERMISSION_DENIED`, with
  a hint to use the latest version. Saved projects behave exactly as before.
- The granted set is set per node version through a context variable (`granting`), so shared helpers check
  against the right version.

### Server: projects as MCP tools
- **Shared builder:** `agenticmind_runtime.mcp_server` turns graphs into MCP tools:
  - the input schema comes from the graph's Manual Input nodes (key, type, default, required, label);
  - arguments are validated with JSON Schema before a run;
  - outputs return as structured content, with text for single outputs;
  - a failed run is an `isError` result.
  - `mcp` is an optional extra of the runtime: `agenticmind-runtime[mcp]`.
- **`/api/mcp`** (Streamable HTTP, stateless, JSON responses):
  - **What is exposed:** a project's latest revision, when `metadata.mcp.expose` is true. The IDE's
    *Expose as MCP tool* switch sets it; `tool_name` is optional.
  - **Calls:** each call is an ordinary recorded run with a trace. A call waits up to
    `AGENTICMIND_MCP_RUN_TIMEOUT` (300 s by default) and then answers "still running".
  - **Configuration:** `AGENTICMIND_MCP_SERVER=0` turns it off. Storing the switch in the IR's free-form
    `metadata` avoids an IR schema change.
- **Access:**
  - The normal `/api` rules apply: cross-site writes are refused, and a session is needed when sign-in is
    required.
  - With `AGENTICMIND_MCP_TOKEN` set, MCP clients send `Authorization: Bearer <token>` instead. The token is
    then required for `/api/mcp` even with sign-in off, and compared in constant time.
  - Host headers are checked against `AGENTICMIND_MCP_ALLOWED_HOSTS` (localhost by default) to block DNS
    rebinding.
- **`agenticmind mcp-serve a.json b.json`:** serves IR files over stdio, for desktop MCP clients and exported
  projects. Problems go to stderr, never into the protocol stream.

### Template
**Agent with MCP tools**: an Agent using this server's own `/api/mcp`, so any exposed project becomes a tool.

## Verification
- **Against a real stdio MCP server** (a fixture run with the test Python):
  - listing with prefix, include and exclude, and unsafe names made safe;
  - an Agent calling two MCP tools in one turn, one of them failing visibly;
  - an older Agent refusing stdio tools;
  - MCP Call with a secret environment variable, while the parent's `OPENROUTER_API_KEY` stays invisible to
    the server;
  - errors, plus Tool Router 1.1.0 with MCP tools;
  - connection failures and config validation.
- **Server:**
  - the in-memory SDK client (list, call, argument validation, failed run, unknown tool);
  - `agenticmind mcp-serve` driven by our own MCP client over stdio;
  - `/api/mcp` in a real uvicorn server: only the exposed project is listed, a call becomes a recorded run,
    and the token is enforced (401 for missing or wrong). A cross-site write is refused (403), and other
    routes are unaffected.
- **Found by the tests:** SDK servers wrap plain return values as `{"result": …}`, now unwrapped.
- **Live (OpenRouter / Gemini):** an Agent used the demo server's tools with function calling and with ReAct
  (1234.5 + 98.25 = 1332.75; 5 words).

## Consequences
- **Performance:** per-call connections make stdio tools slower (a process start per call). A connection pool
  scoped to a run is the next improvement.
- **Not supported yet:**
  - MCP resources and prompts (client and server);
  - sampling and elicitation callbacks;
  - OAuth for remote MCP servers (bearer tokens and headers only).
- **Trust:** stdio MCP servers run with the user's privileges outside any sandbox. Only add servers you trust.
  The sandbox (next feature) is where this tightens.

## Revisit when
Run-scoped connection pooling, OAuth, resources and prompts, or running stdio servers inside the sandbox are
needed.

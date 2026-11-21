"""A tiny MCP server over stdio for tests: arithmetic, an env echo and a failing tool."""

from __future__ import annotations

import os

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("demo")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@mcp.tool(name="word.count")
def word_count(text: str) -> dict:
    """Count the words in a text."""
    return {"words": len(text.split())}


@mcp.tool()
def read_env(name: str) -> str:
    """Return an environment variable of the server process (empty if unset)."""
    return os.environ.get(name, "")


@mcp.tool()
def explode() -> str:
    """Always fails."""
    raise ToolError("kaboom: the demo tool failed")


if __name__ == "__main__":
    mcp.run("stdio")

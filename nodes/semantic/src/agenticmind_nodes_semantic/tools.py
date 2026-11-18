"""Agent tool implementation for the SQL Tool node (discovered via the ``agenticmind.tools`` entry point)."""

from __future__ import annotations

import asyncio
from typing import Any

from agenticmind_node_sdk import DatabaseHandle, NodeContext, NodeError, ToolHandle

from .db import open_database
from .nodes import run_checked


class ToolError(Exception):
    """Mirrors agenticmind_nodes_agents.tools.ToolError: the message goes back to the model."""


async def run_sql_tool(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    handle = DatabaseHandle.model_validate(tool.binding["database"])
    db = await open_database(ctx, handle)
    try:
        out = await asyncio.to_thread(
            run_checked, db, str(args.get("sql", "")), max_rows=int(tool.binding.get("max_rows", 50))
        )
    except NodeError as exc:
        # policy and SQL errors are for the model to fix, not node failures
        return {"error": f"{exc.code}: {exc.message}"}
    finally:
        db.close()
    return {k: out[k] for k in ("sql", "columns", "rows", "row_count", "truncated", "notes")}

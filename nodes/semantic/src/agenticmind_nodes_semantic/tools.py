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


async def run_ontology_tool(ctx: NodeContext, tool: ToolHandle, args: dict[str, Any]) -> Any:
    """Auto Ontology Tool: ``search_terms`` or ``ask_question`` (ADR-0019)."""
    from .ontology_nodes import answer_result, client_for, term_line

    action = args.get("action")
    client, handle = client_for(ctx, tool.binding["ontology"])
    try:
        async with client:
            if action == "search_terms":
                page = await client.list_terms(str(args.get("query") or "") or None, 25)
                terms = [t for t in page.get("terms", []) if isinstance(t, dict)]
                return {"terms": [term_line(t)[2:] for t in terms], "total": page.get("total", len(terms))}
            if action == "ask_question":
                question = str(args.get("question") or "").strip()
                if not question:
                    return {"error": "ask_question needs a question"}
                ans = await client.ask(
                    question, target_db=handle.target_db, timeout=float(tool.binding.get("timeout_seconds", 300))
                )
                result = answer_result(ans, int(tool.binding.get("max_rows", 50)))
                return {k: result[k] for k in ("answer", "sql", "columns", "rows", "row_count", "truncated")}
            return {"error": "action must be search_terms or ask_question"}
    except NodeError as exc:
        if exc.code in ("ONTOLOGY_UNAUTHORIZED", "ONTOLOGY_FORBIDDEN"):
            raise  # configuration problems are node failures, not something the model can fix
        return {"error": f"{exc.code}: {exc.message}"}

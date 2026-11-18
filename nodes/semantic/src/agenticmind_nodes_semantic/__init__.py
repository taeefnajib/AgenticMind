"""AgenticMind semantic data nodes (blueprint §9, ADR-0018) and the Auto Ontology adapter (ADR-0019)."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .nodes import database, nl_to_sql, schema_inspector, sql_executor, sql_tool, table_profiler
from .ontology_nodes import ONTOLOGY_NODES

ALL_NODES: tuple[NodeDefinition[Any], ...] = (
    database,
    schema_inspector,
    table_profiler,
    sql_executor,
    nl_to_sql,
    sql_tool,
    *ONTOLOGY_NODES,
)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "register"]

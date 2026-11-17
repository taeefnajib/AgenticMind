"""AgenticMind agent node package: the Agent node and the tools it can use."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .agent import agent
from .tools import TOOL_IMPLEMENTATIONS, calculator_tool, datetime_tool, knowledge_search_tool, web_fetch_tool

ALL_NODES: tuple[NodeDefinition[Any], ...] = (
    agent,
    knowledge_search_tool,
    calculator_tool,
    datetime_tool,
    web_fetch_tool,
)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "TOOL_IMPLEMENTATIONS", "register"]

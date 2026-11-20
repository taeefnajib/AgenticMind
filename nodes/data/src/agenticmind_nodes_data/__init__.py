"""AgenticMind data ingestion node package."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .nodes import document_cleaner, document_loader, image_loader, ocr

ALL_NODES: tuple[NodeDefinition[Any], ...] = (document_loader, document_cleaner, image_loader, ocr)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "register"]

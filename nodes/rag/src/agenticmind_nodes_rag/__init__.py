"""AgenticMind RAG node package."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .chunker import chunker
from .retrieval import embedding_model, index_documents, retriever, vector_store

ALL_NODES: tuple[NodeDefinition[Any], ...] = (chunker, embedding_model, vector_store, index_documents, retriever)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "register"]

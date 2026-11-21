"""AgenticMind RAG node package."""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import NodeDefinition

from .advanced import context_compressor, hybrid_search, keyword_search, query_rewriter, reranker
from .answering import citation_builder, context_builder
from .chunker import chunker, chunker_v11
from .parents import parent_expander
from .retrieval import embedding_model, index_documents, index_documents_v1, retriever, vector_store

ALL_NODES: tuple[NodeDefinition[Any], ...] = (
    chunker_v11,
    chunker,
    embedding_model,
    vector_store,
    index_documents,
    index_documents_v1,
    retriever,
    keyword_search,
    hybrid_search,
    query_rewriter,
    reranker,
    context_compressor,
    context_builder,
    citation_builder,
    parent_expander,
)


def register(registrar: Any) -> None:
    """Entry point for the ``agenticmind.nodes`` group."""
    for definition in ALL_NODES:
        registrar(definition)


__all__ = ["ALL_NODES", "register"]

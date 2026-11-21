"""Parent Expander (ADR-0027): small chunks find the passage, their parent section answers.

Pairs with the Chunker's ``parent_child`` strategy: children are indexed (precise matching), and each
child carries ``parent_chunk_id``, ``parent_text`` and the parent's offsets in its metadata, so this node
needs no second store. Retrieved children are replaced by their parents, de-duplicated, in rank order;
each parent keeps the best score of its children. Documents without parent metadata pass through.
"""

from __future__ import annotations

from typing import Any

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
)
from pydantic import Field

from .chunker import PACKAGE


class ParentExpanderConfig(NodeConfig):
    max_parents: int = Field(default=5, ge=1, le=100, description="Return at most this many parent sections.")


@define_node(
    type="agenticmind.rag.parent_expander",
    version="1.0.0",
    display_name="Parent Expander",
    category="rag",
    package=PACKAGE,
    description="Replaces retrieved small chunks with their larger parent sections (parent-child chunking): "
    "precise matching, fuller context for the answer.",
    inputs=[InputPort(name="documents", type=PortType.DOCUMENT_SET, description="Retrieved chunks")],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET, description="Parent sections, best first")],
    config=ParentExpanderConfig,
    tags=["rag", "retrieval"],
)
async def parent_expander(ctx: NodeContext, inputs, config: ParentExpanderConfig) -> NodeResult:
    out: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for doc in inputs["documents"]:
        meta = doc.get("metadata") or {}
        pid = meta.get("parent_chunk_id")
        if not pid or not isinstance(meta.get("parent_text"), str):
            out.append(doc)  # not from a parent-child chunker: keep as is
            continue
        if pid in index:
            entry = index[pid]
            entry["metadata"]["matched_chunks"].append(doc["id"])
            for key in ("score", "rerank_score"):
                if isinstance(doc.get(key), (int, float)) and doc[key] > (entry.get(key) or float("-inf")):
                    entry[key] = doc[key]
            continue
        if len([d for d in out if (d.get("metadata") or {}).get("chunk_strategy") == "parent"]) >= config.max_parents:
            continue
        rest = {k: v for k, v in meta.items() if k not in ("parent_text", "parent_chunk_id", "parent_start",
                                                             "parent_end", "chunk_index", "chunk_count",
                                                             "chunk_size", "char_count")}  # fmt: skip
        parent = {
            **{k: v for k, v in doc.items() if k not in ("id", "content", "offsets", "metadata")},
            "id": pid,
            "content": meta["parent_text"],
            "metadata": {
                **rest,
                "chunk_strategy": "parent",
                "char_count": len(meta["parent_text"]),
                "matched_chunks": [doc["id"]],
            },  # fmt: skip
        }
        if isinstance(meta.get("parent_start"), int) and isinstance(meta.get("parent_end"), int):
            parent["offsets"] = {"start": meta["parent_start"], "end": meta["parent_end"]}
        index[pid] = parent
        out.append(parent)
    await ctx.emit("parents_expanded", {"chunks": len(inputs["documents"]), "parents": len(index)})
    return NodeResult(outputs={"documents": out})

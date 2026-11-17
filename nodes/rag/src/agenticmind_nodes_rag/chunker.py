"""Chunker node."""

from __future__ import annotations

from typing import Literal

from agenticmind_node_sdk import (
    Document,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeResult,
    Offsets,
    OutputPort,
    PortType,
    define_node,
    ui,
)
from agenticmind_node_sdk.documents import parse_documents, to_values
from pydantic import Field, model_validator

from .chunking import DEFAULT_SEPARATORS, Measurer, chunk_text

PACKAGE = "agenticmind-nodes-rag"
CHUNKER = "agenticmind.rag.chunker@1.0.0"


class ChunkerConfig(NodeConfig):
    strategy: Literal["recursive", "fixed", "markdown"] = Field(
        default="recursive",
        description="recursive: split on paragraphs → lines → sentences → words. fixed: sliding window. "
        "markdown: split by headings first (keeps the section path).",
    )
    unit: Literal["tokens", "characters"] = Field(
        default="tokens", description="Size unit. Tokens are approximate (words and punctuation)."
    )
    chunk_size: int = Field(default=300, ge=1, le=100_000)
    chunk_overlap: int = Field(default=50, ge=0, le=99_999)
    separators: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SEPARATORS),
        description="Recursive separators, coarsest first.",
        json_schema_extra=ui(advanced=True),
    )

    @model_validator(mode="after")
    def _overlap_smaller(self) -> ChunkerConfig:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


@define_node(
    type="agenticmind.rag.chunker",
    version="1.0.0",
    display_name="Chunker",
    category="rag",
    package=PACKAGE,
    description="Splits documents into overlapping chunks, preserving exact source offsets and metadata.",
    inputs=[InputPort(name="documents", type=PortType.DOCUMENT_SET)],
    outputs=[OutputPort(name="chunks", type=PortType.DOCUMENT_SET)],
    config=ChunkerConfig,
    tags=["rag", "ingestion"],
)
async def chunker(ctx: NodeContext, inputs, config: ChunkerConfig) -> NodeResult:
    out: list[Document] = []
    for doc in parse_documents(inputs["documents"]):
        spans = chunk_text(
            doc.content,
            strategy=config.strategy,
            unit=config.unit,
            size=config.chunk_size,
            overlap=config.chunk_overlap,
            separators=config.separators,
        )
        measure = Measurer(doc.content, config.unit)
        for i, span in enumerate(spans):
            source = doc.source
            if span.section:
                source = source.model_copy(update={"section": span.section})
            out.append(
                Document(
                    id=f"{doc.id}#c{i}",
                    content=doc.content[span.start : span.end],
                    source=source,
                    parent_id=doc.id,
                    offsets=Offsets(start=span.start, end=span.end),
                    metadata={
                        **doc.metadata,
                        "chunk_index": i,
                        "chunk_count": len(spans),
                        "chunk_strategy": config.strategy,
                        "chunk_size": measure(span.start, span.end),
                        "chunk_unit": config.unit,
                        "char_count": span.end - span.start,
                    },
                    ingestion_version=f"{doc.ingestion_version} > {CHUNKER}" if doc.ingestion_version else CHUNKER,
                )
            )
    await ctx.emit("chunked", {"documents": len(inputs["documents"]), "chunks": len(out)})
    return NodeResult(outputs={"chunks": to_values(out)})

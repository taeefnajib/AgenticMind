"""Chunker nodes: 1.0.0 (recursive / fixed / markdown) and 1.1.0 (all strategies, ADR-0027)."""

from __future__ import annotations

from typing import Literal

from agenticmind_node_sdk import (
    Document,
    EmbeddingModelHandle,
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    Offsets,
    OutputPort,
    Permissions,
    PortType,
    define_node,
    ui,
)
from agenticmind_node_sdk.documents import parse_documents, to_values
from pydantic import Field, model_validator

from .chunking import (
    DEFAULT_SEPARATORS,
    Chunk,
    Measurer,
    _finalize,
    chunk_text,
    layout_chunks,
    lexical_vectors,
    pack_spans,
    parent_child_chunks,
    recursive_spans,
    semantic_spans,
    sentence_spans,
    sentence_windows,
)

PACKAGE = "agenticmind-nodes-rag"
CHUNKER = "agenticmind.rag.chunker@1.0.0"
CHUNKER_V11 = "agenticmind.rag.chunker@1.1.0"


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


# -- Chunker 1.1.0 --------------------------------------------------------------------------------
Strategy = Literal["recursive", "fixed", "sentence", "markdown", "layout", "semantic", "document", "parent_child"]


def _on(*strategies: str) -> dict[str, list[str]]:
    return {"strategy": list(strategies)}


class ChunkerConfigV11(NodeConfig):
    strategy: Strategy = Field(
        default="recursive",
        description="recursive: character/token splitting on paragraphs → lines → sentences → words. fixed: "
        "sliding window. sentence: whole sentences packed together. markdown: by headings. layout: follows the "
        "document's structure (headings stay with their content; tables, lists and code are not cut). semantic: "
        "splits where the topic changes. document: one chunk per document (split only if too long). "
        "parent_child: small chunks for retrieval, each linked to a larger parent section for the answer.",
    )
    unit: Literal["tokens", "characters"] = Field(
        default="tokens", description="Size unit. Tokens are approximate (words and punctuation)."
    )
    chunk_size: int = Field(
        default=300, ge=1, le=100_000, description="Maximum chunk size (children for parent_child)."
    )
    chunk_overlap: int = Field(
        default=50, ge=0, le=99_999,
        json_schema_extra=ui(show_when=_on("recursive", "fixed", "sentence", "markdown", "parent_child")),
    )  # fmt: skip
    parent_chunk_size: int = Field(
        default=1200, ge=2, le=200_000, description="Size of the parent sections returned to the model.",
        json_schema_extra=ui(show_when=_on("parent_child")),
    )  # fmt: skip
    semantic_threshold: int = Field(
        default=90,
        ge=50,
        le=99,
        description="Percentile of sentence-to-sentence dissimilarity treated as a topic change (higher = fewer, "
        "larger chunks).",
        json_schema_extra=ui(show_when=_on("semantic")),
    )
    min_chunk_size: int = Field(
        default=40, ge=0, le=100_000, description="Smaller semantic groups are merged with a neighbour.",
        json_schema_extra=ui(show_when=_on("semantic"), advanced=True),
    )  # fmt: skip
    separators: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SEPARATORS),
        description="Recursive separators, coarsest first.",
        json_schema_extra=ui(advanced=True, show_when=_on("recursive", "markdown", "parent_child")),
    )

    @model_validator(mode="after")
    def _check(self) -> ChunkerConfigV11:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if self.strategy == "parent_child" and self.parent_chunk_size <= self.chunk_size:
            raise ValueError("parent_chunk_size must be larger than chunk_size")
        return self


def _chunk_doc(
    doc: Document, spans: list[Chunk], config: ChunkerConfigV11, extra: dict | None = None
) -> list[Document]:
    measure = Measurer(doc.content, config.unit)
    out = []
    for i, span in enumerate(spans):
        source = doc.source.model_copy(update={"section": span.section}) if span.section else doc.source
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
                    **(extra or {}),
                },
                ingestion_version=f"{doc.ingestion_version} > {CHUNKER_V11}" if doc.ingestion_version else CHUNKER_V11,
            )
        )
    return out


@define_node(
    type="agenticmind.rag.chunker",
    version="1.1.0",
    display_name="Chunker",
    category="rag",
    package=PACKAGE,
    description="Splits documents into chunks: recursive, fixed, sentence, markdown, layout-aware, semantic, "
    "whole-document or parent-child. Every chunk keeps exact source offsets for citations.",
    inputs=[
        InputPort(name="documents", type=PortType.DOCUMENT_SET),
        InputPort(
            name="embedding_model",
            type=PortType.EMBEDDING_MODEL,
            required=False,
            description="semantic: use these embeddings to find topic changes (otherwise word overlap)",
        ),
    ],
    outputs=[
        OutputPort(
            name="chunks", type=PortType.DOCUMENT_SET, description="Chunks to index (children for parent_child)"
        ),
        OutputPort(name="parents", type=PortType.DOCUMENT_SET, description="parent_child: the parent sections"),
    ],
    config=ChunkerConfigV11,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=600),
    tags=["rag", "ingestion"],
)
async def chunker_v11(ctx: NodeContext, inputs, config: ChunkerConfigV11) -> NodeResult:
    docs = parse_documents(inputs["documents"])
    model = EmbeddingModelHandle.model_validate(inputs["embedding_model"]) if inputs.get("embedding_model") else None
    out: list[Document] = []
    parents: list[Document] = []
    seps = config.separators
    for doc in docs:
        text = doc.content
        measure = Measurer(text, config.unit)
        size, overlap = config.chunk_size, config.chunk_overlap
        if config.strategy in ("recursive", "fixed", "markdown"):
            spans = chunk_text(
                text, strategy=config.strategy, unit=config.unit, size=size, overlap=overlap, separators=seps
            )
        elif config.strategy == "document":
            whole = _finalize(text, [(0, len(text))])
            fits = bool(whole) and measure(*whole[0]) <= size
            spans = (
                [Chunk(*whole[0])]
                if fits
                else [
                    Chunk(a, b) for a, b in _finalize(text, recursive_spans(text, 0, len(text), size, 0, measure, seps))
                ]
            )
        elif config.strategy == "sentence":
            spans = [
                Chunk(a, b)
                for a, b in pack_spans(text, sentence_spans(text, 0, len(text)), size, overlap, measure, seps)
            ]
        elif config.strategy == "layout":
            spans = layout_chunks(text, size, measure, seps)
        elif config.strategy == "semantic":
            sentences = sentence_spans(text, 0, len(text))
            windows = sentence_windows(text, sentences)
            if model is not None and len(windows) > 1:
                from .retrieval import embed_texts

                vectors = (await embed_texts(ctx, model, windows, "document")).vectors
                method = model.model_id
            else:
                vectors, method = lexical_vectors(windows), "lexical"
            pairs, breaks = semantic_spans(
                text, sentences, vectors, size=size, min_size=config.min_chunk_size,
                percentile=config.semantic_threshold, measure=measure, separators=seps,
            )  # fmt: skip
            spans = [Chunk(a, b) for a, b in pairs]
            await ctx.emit("semantic_breaks", {"document": doc.id, "sentences": len(sentences), "breaks": len(breaks),
                                               "similarity": method})  # fmt: skip
        elif config.strategy == "parent_child":
            spans = []
            owners: list[Document] = []  # the parent of each child, aligned with spans
            for p_index, ((ps, pe), children) in enumerate(
                parent_child_chunks(
                    text,
                    parent_size=config.parent_chunk_size,
                    size=size,
                    overlap=overlap,
                    measure=measure,
                    separators=seps,
                )  # fmt: skip
            ):
                parent_id = f"{doc.id}#p{p_index}"
                parents.append(
                    Document(
                        id=parent_id, content=text[ps:pe], source=doc.source, parent_id=doc.id,
                        offsets=Offsets(start=ps, end=pe),
                        metadata={**doc.metadata, "chunk_strategy": "parent", "char_count": pe - ps},
                        ingestion_version=CHUNKER_V11,
                    )
                )  # fmt: skip
                spans.extend(Chunk(a, b) for a, b in children)
                owners.extend([parents[-1]] * len(children))
        else:  # pragma: no cover - guarded by the config Literal
            raise NodeError("CONFIG_INVALID", f"Unknown strategy {config.strategy}")
        if config.strategy == "parent_child":
            docs_out = _chunk_doc(doc, spans, config)
            for d, p in zip(docs_out, owners, strict=True):
                d.metadata.update({"parent_chunk_id": p.id, "parent_text": p.content,
                                   "parent_start": p.offsets.start, "parent_end": p.offsets.end})  # fmt: skip
            out.extend(docs_out)
        else:
            out.extend(_chunk_doc(doc, spans, config))
    await ctx.emit("chunked", {"documents": len(docs), "chunks": len(out), "parents": len(parents),
                               "strategy": config.strategy})  # fmt: skip
    return NodeResult(outputs={"chunks": to_values(out), "parents": to_values(parents)})

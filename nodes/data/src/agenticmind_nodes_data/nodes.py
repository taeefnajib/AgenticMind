"""Data ingestion nodes: Document Loader and Document Cleaner."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Literal

from agenticmind_node_sdk import (
    Document,
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SourceRef,
    define_node,
    ui,
)
from agenticmind_node_sdk.documents import parse_documents, to_values
from pydantic import Field

from .parsers import ParseError, parse_bytes

PACKAGE = "agenticmind-nodes-data"
LOADER = "agenticmind.data.document_loader@1.0.0"
CLEANER = "agenticmind.data.document_cleaner@1.0.0"


class DocumentLoaderConfig(NodeConfig):
    files: list[str] = Field(
        default_factory=list,
        description="Project files to load. Upload files in the project panel or here.",
        json_schema_extra=ui(widget="artifacts"),
    )
    split_pdf_pages: bool = Field(default=True, description="One document per PDF page (enables page citations).")
    csv_mode: Literal["file", "rows"] = Field(
        default="file",
        description="'rows' makes one document per CSV row.",
        json_schema_extra=ui(advanced=True),
    )
    skip_empty: bool = Field(
        default=True, description="Drop parts with no text (e.g. blank pages).", json_schema_extra=ui(advanced=True)
    )


@define_node(
    type="agenticmind.data.document_loader",
    version="1.0.0",
    display_name="Document Loader",
    category="data",
    package=PACKAGE,
    description="Loads and parses uploaded files (PDF, Word, Excel, PowerPoint, EPUB, Markdown, HTML, "
    "CSV/TSV, JSON/JSONL, XML, YAML, text) into documents.",
    inputs=[
        InputPort(
            name="files",
            type=PortType.JSON,
            required=False,
            description="Extra file IDs (list of strings), e.g. from a run input",
        )
    ],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET)],
    config=DocumentLoaderConfig,
    permissions=Permissions(artifacts=True),
    execution=ExecutionPolicy(timeout_seconds=300),
    tags=["ingestion", "rag"],
)
async def document_loader(ctx: NodeContext, inputs, config: DocumentLoaderConfig) -> NodeResult:
    extra = inputs.get("files")
    if extra is not None and not (isinstance(extra, list) and all(isinstance(x, str) for x in extra)):
        raise NodeError("INPUT_TYPE_MISMATCH", "'files' input must be a list of file IDs")
    file_ids = list(dict.fromkeys([*config.files, *(extra or [])]))
    if not file_ids:
        raise NodeError("NO_FILES", "Select at least one file to load")

    documents: list[Document] = []
    for artifact_id in file_ids:
        info = ctx.artifact_info(artifact_id)
        data = ctx.read_artifact(artifact_id)
        try:
            parts = await asyncio.to_thread(
                parse_bytes, data, info.filename, split_pdf_pages=config.split_pdf_pages, csv_mode=config.csv_mode
            )
        except ParseError as exc:
            raise NodeError(exc.code, f"{info.filename}: {exc}") from exc
        kept = [p for p in parts if p.content.strip()] if config.skip_empty else parts
        if not kept:
            raise NodeError(
                "NO_TEXT_EXTRACTED",
                f"{info.filename}: no text could be extracted (scanned PDFs need OCR, not yet supported)",
            )
        for part in kept:
            documents.append(
                Document(
                    id=f"{artifact_id}{part.id_suffix}",
                    content=part.content,
                    source=SourceRef(
                        uri=f"artifact://{artifact_id}/{info.filename}",
                        filename=info.filename,
                        media_type=info.media_type,
                        artifact_id=artifact_id,
                        page=part.page,
                        section=part.section,
                    ),
                    metadata={"sha256": info.sha256, "char_count": len(part.content), **part.metadata},
                    ingestion_version=LOADER,
                )
            )
        await ctx.emit(
            "document_loaded",
            {"filename": info.filename, "parts": len(kept), "chars": sum(len(p.content) for p in kept)},
        )
    return NodeResult(outputs={"documents": to_values(documents)})


class DocumentCleanerConfig(NodeConfig):
    normalize_unicode: bool = Field(default=True, description="NFKC-normalise (ligatures, full-width chars…).")
    remove_control_chars: bool = True
    dehyphenate: bool = Field(default=True, description="Join words split across lines ('exam-\\nple' → 'example').")
    collapse_whitespace: bool = Field(default=True, description="Collapse runs of spaces; keep paragraph breaks.")
    drop_empty: bool = Field(default=True, description="Remove documents that are empty after cleaning.")
    min_chars: int = Field(
        default=0, ge=0, description="Drop documents shorter than this.", json_schema_extra=ui(advanced=True)
    )


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def clean_text(text: str, config: DocumentCleanerConfig) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if config.normalize_unicode:
        text = unicodedata.normalize("NFKC", text)
    if config.remove_control_chars:
        text = _CONTROL.sub("", text)
    if config.dehyphenate:
        text = _HYPHEN_BREAK.sub(r"\1\2", text)
    if config.collapse_whitespace:
        text = re.sub(r"[ \t\f\v ]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@define_node(
    type="agenticmind.data.document_cleaner",
    version="1.0.0",
    display_name="Document Cleaner",
    category="data",
    package=PACKAGE,
    description="Normalises and cleans document text before chunking.",
    inputs=[InputPort(name="documents", type=PortType.DOCUMENT_SET)],
    outputs=[OutputPort(name="documents", type=PortType.DOCUMENT_SET)],
    config=DocumentCleanerConfig,
    tags=["ingestion", "rag"],
)
async def document_cleaner(ctx: NodeContext, inputs, config: DocumentCleanerConfig) -> NodeResult:
    out: list[Document] = []
    dropped = 0
    for doc in parse_documents(inputs["documents"]):
        content = clean_text(doc.content, config)
        if (config.drop_empty and not content) or len(content) < config.min_chars:
            dropped += 1
            continue
        out.append(
            doc.model_copy(
                update={
                    "content": content,
                    "metadata": {**doc.metadata, "cleaned": True, "char_count": len(content)},
                    "ingestion_version": f"{doc.ingestion_version} > {CLEANER}" if doc.ingestion_version else CLEANER,
                }
            )
        )
    if dropped:
        await ctx.emit("documents_dropped", {"count": dropped})
    return NodeResult(outputs={"documents": to_values(out)})

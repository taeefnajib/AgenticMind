"""Data ingestion nodes: Document Loader (with OCR), Document Cleaner, Image Loader and OCR."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import unicodedata
from pathlib import Path
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
    SecretRef,
    SourceRef,
    coerce_images,
    define_node,
    image_ref,
    secret_field,
    ui,
)
from agenticmind_node_sdk.documents import parse_documents, to_values
from agenticmind_node_sdk.images import IMAGE_MEDIA_TYPES, is_image_media_type
from pydantic import Field, model_validator

from .ocr_engine import ENGINE_NAME, ocr_bytes, pdf_page_images
from .parsers import SUPPORTED_EXTENSIONS, ParsedPart, ParseError, parse_bytes

PACKAGE = "agenticmind-nodes-data"
LOADER = "agenticmind.data.document_loader@1.0.0"
CLEANER = "agenticmind.data.document_cleaner@1.0.0"
OCR_NODE = "agenticmind.data.ocr@1.0.0"


class DocumentLoaderConfig(NodeConfig):
    files: list[str] = Field(
        default_factory=list,
        description="Project files to load. Upload files in the project panel or here.",
        json_schema_extra=ui(widget="artifacts"),
    )
    split_pdf_pages: bool = Field(default=True, description="One document per PDF page (enables page citations).")
    pdf_layout: bool = Field(
        default=False,
        description="Layout-aware PDF parsing: turn larger-font lines into headings (pair with the Chunker's "
        "layout or markdown strategy).",
        json_schema_extra=ui(advanced=True),
    )
    csv_mode: Literal["file", "rows"] = Field(
        default="file",
        description="'rows' makes one document per CSV row.",
        json_schema_extra=ui(advanced=True),
    )
    skip_empty: bool = Field(
        default=True, description="Drop parts with no text (e.g. blank pages).", json_schema_extra=ui(advanced=True)
    )
    ocr: Literal["auto", "off"] = Field(
        default="auto",
        description="auto: read text from images and from scanned PDF pages (pages without a text layer) "
        "with open-source OCR. off: never run OCR.",
    )
    ocr_min_confidence: float = Field(
        default=0.5,
        ge=0,
        le=1,
        description="Drop OCR lines below this confidence.",
        json_schema_extra=ui(advanced=True),
    )


def _is_image_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_MEDIA_TYPES


def _ocr_parts(data: bytes, filename: str, parts: list[ParsedPart], min_confidence: float) -> list[ParsedPart]:
    """Fill empty PDF pages (no text layer) with OCR of their embedded images; OCR whole image files."""
    if _is_image_file(filename):
        result = ocr_bytes(data, min_confidence=min_confidence)
        return [ParsedPart(content=result.text, metadata=_ocr_meta(result))]
    if Path(filename).suffix.lower() != ".pdf":
        return parts
    empty = {p.page for p in parts if p.page is not None and not p.content.strip()}
    if not empty:
        if len(parts) == 1 and parts[0].page is None and not parts[0].content.strip():
            # pages joined into one document and none had text: OCR every page
            count = int(parts[0].metadata.get("page_count") or 0)
            images = pdf_page_images(data, set(range(1, count + 1)))
            texts = [ocr_bytes(img, min_confidence=min_confidence) for n in sorted(images) for img in images[n]]
            text = "\n\n".join(t.text for t in texts if t.text)
            return [ParsedPart(content=text, metadata={**parts[0].metadata, "ocr": ENGINE_NAME, "ocr_pages": count})]
        return parts
    images = pdf_page_images(data, empty)
    out = []
    for part in parts:
        if part.page in empty and images.get(part.page):
            results = [ocr_bytes(img, min_confidence=min_confidence) for img in images[part.page]]
            text = "\n".join(r.text for r in results if r.text)
            meta = {**part.metadata, **_ocr_meta(results[0])} if results else part.metadata
            out.append(ParsedPart(content=text, page=part.page, section=part.section, metadata=meta,
                                  id_suffix=part.id_suffix))  # fmt: skip
        else:
            out.append(part)
    return out


def _ocr_meta(result) -> dict:
    return {"ocr": ENGINE_NAME, "ocr_lines": len(result.lines), "ocr_confidence": result.mean_confidence}


async def parse_file(data: bytes, filename: str, config: DocumentLoaderConfig | S3LoaderConfig) -> list[ParsedPart]:
    """Bytes of one file → text parts, with OCR for images and scanned pages (shared by the file loaders)."""
    try:
        if _is_image_file(filename):
            if config.ocr == "off":
                raise ParseError("OCR_OFF", "this is an image: set OCR to 'auto' to read its text")
            parts: list[ParsedPart] = []
        else:
            parts = await asyncio.to_thread(
                parse_bytes,
                data,
                filename,
                split_pdf_pages=config.split_pdf_pages,
                csv_mode=config.csv_mode,
                pdf_layout=config.pdf_layout,
            )
        if config.ocr == "auto":
            parts = await asyncio.to_thread(_ocr_parts, data, filename, parts, config.ocr_min_confidence)
    except ParseError as exc:
        raise NodeError(exc.code, f"{filename}: {exc}") from exc
    kept = [p for p in parts if p.content.strip()] if config.skip_empty else parts
    if not kept:
        hint = "" if config.ocr == "auto" else " (scanned PDFs and images need OCR: set OCR to 'auto')"
        raise NodeError("NO_TEXT_EXTRACTED", f"{filename}: no text could be extracted{hint}")
    return kept


@define_node(
    type="agenticmind.data.document_loader",
    version="1.0.0",
    display_name="Document Loader",
    category="data",
    package=PACKAGE,
    description="Loads and parses uploaded files (PDF, Word, Excel, PowerPoint, EPUB, Markdown, HTML, "
    "CSV/TSV, JSON/JSONL, XML, YAML, text, and images or scanned PDFs via OCR) into documents.",
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
        kept = await parse_file(data, info.filename, config)
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


# -- Image Loader ---------------------------------------------------------------------------------
class ImageLoaderConfig(NodeConfig):
    files: list[str] = Field(
        default_factory=list,
        description="Images to load (PNG, JPEG, WebP, GIF; BMP/TIFF for OCR). Upload them in the project panel.",
        json_schema_extra=ui(widget="artifacts"),
    )


@define_node(
    type="agenticmind.data.image_loader",
    version="1.0.0",
    display_name="Image Loader",
    category="data",
    package=PACKAGE,
    description="Loads uploaded images for vision models (LLM image inputs) or OCR.",
    inputs=[InputPort(name="files", type=PortType.JSON, required=False, description="Extra image file IDs (list)")],
    outputs=[
        OutputPort(name="image", type=PortType.IMAGE, description="The first image"),
        OutputPort(name="images", type=PortType.JSON, description="All images (list of image references)"),
    ],
    config=ImageLoaderConfig,
    permissions=Permissions(artifacts=True),
    tags=["ingestion", "vision"],
)
async def image_loader(ctx: NodeContext, inputs, config: ImageLoaderConfig) -> NodeResult:
    extra = inputs.get("files")
    if extra is not None and not (isinstance(extra, list) and all(isinstance(x, str) for x in extra)):
        raise NodeError("INPUT_TYPE_MISMATCH", "'files' input must be a list of file IDs")
    file_ids = list(dict.fromkeys([*config.files, *(extra or [])]))
    if not file_ids:
        raise NodeError("NO_FILES", "Select at least one image")
    refs = []
    for artifact_id in file_ids:
        info = ctx.artifact_info(artifact_id)
        if not is_image_media_type(info.media_type):
            raise NodeError("NOT_AN_IMAGE", f"{info.filename} is not an image ({info.media_type})")
        refs.append(image_ref(info))
    return NodeResult(outputs={"image": refs[0], "images": refs})


# -- OCR ------------------------------------------------------------------------------------------
class OcrConfig(NodeConfig):
    files: list[str] = Field(
        default_factory=list,
        description="Images or scanned PDFs to read (in addition to the image inputs).",
        json_schema_extra=ui(widget="artifacts"),
    )
    min_confidence: float = Field(default=0.5, ge=0, le=1, description="Drop lines the engine is less sure of.")


@define_node(
    type="agenticmind.data.ocr",
    version="1.0.0",
    display_name="OCR",
    category="data",
    package=PACKAGE,
    description="Reads text from images and scanned PDFs with open-source OCR (RapidOCR, runs locally).",
    inputs=[
        InputPort(name="image", type=PortType.IMAGE, required=False),
        InputPort(name="images", type=PortType.JSON, required=False, description="List of image references"),
    ],
    outputs=[
        OutputPort(name="text", type=PortType.STRING, description="All recognised text"),
        OutputPort(name="documents", type=PortType.DOCUMENT_SET, description="One document per image or page"),
    ],
    config=OcrConfig,
    permissions=Permissions(artifacts=True),
    execution=ExecutionPolicy(timeout_seconds=600),
    tags=["ingestion", "vision", "ocr"],
)
async def ocr(ctx: NodeContext, inputs, config: OcrConfig) -> NodeResult:
    try:
        images = coerce_images(inputs.get("image")) + coerce_images(inputs.get("images"))
    except ValueError as exc:
        raise NodeError("INPUT_TYPE_MISMATCH", str(exc)) from exc
    ids = list(dict.fromkeys([*(i["artifact_id"] for i in images), *config.files]))
    if not ids:
        raise NodeError("NO_FILES", "Connect an image or select files to read")
    documents: list[Document] = []
    for artifact_id in ids:
        info = ctx.artifact_info(artifact_id)
        data = ctx.read_artifact(artifact_id)
        is_pdf = Path(info.filename).suffix.lower() == ".pdf"
        if not is_pdf and not is_image_media_type(info.media_type):
            raise NodeError("NOT_AN_IMAGE", f"{info.filename} is not an image or PDF")
        try:
            if is_pdf:
                pages = await asyncio.to_thread(parse_bytes, data, info.filename, split_pdf_pages=True)
                blank = [ParsedPart(content="", page=p.page, metadata=p.metadata, id_suffix=p.id_suffix) for p in pages]
                parts = await asyncio.to_thread(_ocr_parts, data, info.filename, blank, config.min_confidence)
            else:
                parts = await asyncio.to_thread(_ocr_parts, data, info.filename, [], config.min_confidence)
        except ParseError as exc:
            raise NodeError(exc.code, f"{info.filename}: {exc}") from exc
        for part in parts:
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
                    ),
                    metadata={"sha256": info.sha256, "char_count": len(part.content), **part.metadata},
                    ingestion_version=OCR_NODE,
                )
            )
        await ctx.emit("ocr_done", {"filename": info.filename, "parts": len(parts),
                                     "chars": sum(len(p.content) for p in parts)})  # fmt: skip
    text = "\n\n".join(d.content for d in documents if d.content)
    return NodeResult(outputs={"text": text, "documents": to_values(documents)})


# -- S3 Loader ------------------------------------------------------------------------------------
S3_LOADER = "agenticmind.data.s3_loader@1.0.0"


def _auth(*modes: str) -> dict[str, list[str]]:
    return {"auth": list(modes)}


class S3LoaderConfig(NodeConfig):
    bucket: str = Field(
        default="",
        pattern=r"^$|^[a-zA-Z0-9][a-zA-Z0-9._-]{1,254}$",
        description="Bucket name (no s3:// prefix).",
    )
    prefix: str = Field(default="", description="Only objects under this folder/prefix, e.g. reports/2026/.")
    keys: list[str] = Field(
        default_factory=list, description="Exact object keys to load (instead of listing the prefix)."
    )
    include: list[str] = Field(
        default_factory=list,
        description="File patterns to load when listing, e.g. *.pdf, *.md (empty = every supported file).",
    )
    max_files: int = Field(default=50, ge=1, le=1000, description="Stop after this many objects.")
    max_file_mb: int = Field(default=50, ge=1, le=500, json_schema_extra=ui(advanced=True))
    auth: Literal["keys", "environment", "anonymous"] = Field(
        default="keys",
        description="keys: access key secrets. environment: the API server's own AWS identity (env vars, "
        "~/.aws, instance or task role). anonymous: a public bucket.",
    )
    access_key_id: SecretRef | None = secret_field(
        "Access key ID secret, e.g. AWS_ACCESS_KEY_ID", show_when=_auth("keys")
    )
    secret_access_key: SecretRef | None = secret_field(
        "Secret access key secret, e.g. AWS_SECRET_ACCESS_KEY", show_when=_auth("keys")
    )
    session_token: SecretRef | None = secret_field(
        "Session token secret (temporary credentials)", show_when=_auth("keys")
    )
    region: str | None = Field(default=None, description="e.g. us-east-1 (R2: auto).")
    endpoint_url: str | None = Field(
        default=None,
        description="S3-compatible storage: MinIO http://localhost:9000, Cloudflare R2 "
        "https://<account>.r2.cloudflarestorage.com, Wasabi https://s3.wasabisys.com ... (empty = AWS).",
    )

    # parsing, as in the Document Loader
    split_pdf_pages: bool = Field(default=True, description="One document per PDF page (enables page citations).")
    pdf_layout: bool = Field(
        default=False,
        description="Layout-aware PDF parsing: turn larger-font lines into headings (pair with the Chunker's "
        "layout or markdown strategy).",
        json_schema_extra=ui(advanced=True),
    )
    csv_mode: Literal["file", "rows"] = Field(default="file", json_schema_extra=ui(advanced=True))
    skip_empty: bool = Field(default=True, json_schema_extra=ui(advanced=True))
    ocr: Literal["auto", "off"] = Field(
        default="auto", description="auto: read images and scanned PDFs with open-source OCR."
    )
    ocr_min_confidence: float = Field(default=0.5, ge=0, le=1, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check_s3(self) -> S3LoaderConfig:
        if self.auth == "keys" and (self.access_key_id is None or self.secret_access_key is None):
            raise ValueError("auth 'keys' needs access_key_id and secret_access_key secret references")
        return self


@define_node(
    type="agenticmind.data.s3_loader",
    version="1.0.0",
    display_name="S3 Loader",
    category="data",
    package=PACKAGE,
    description="Loads and parses files from Amazon S3 or S3-compatible storage (MinIO, Cloudflare R2, Wasabi...) "
    "into documents, like the Document Loader (PDF, Office, Markdown, CSV, images via OCR...).",
    inputs=[InputPort(name="keys", type=PortType.JSON, required=False, description="Extra object keys (list)")],
    outputs=[
        OutputPort(name="documents", type=PortType.DOCUMENT_SET),
        OutputPort(name="objects", type=PortType.JSON, description="Objects loaded: key, size, etag, last_modified"),
    ],
    config=S3LoaderConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=900, max_attempts=2),
    tags=["ingestion", "rag", "cloud"],
)
async def s3_loader(ctx: NodeContext, inputs, config: S3LoaderConfig) -> NodeResult:
    from . import s3

    if not config.bucket:
        raise NodeError("S3_CONFIG_INVALID", "Enter a bucket name")
    extra = inputs.get("keys")
    if extra is not None and not (isinstance(extra, list) and all(isinstance(x, str) for x in extra)):
        raise NodeError("INPUT_TYPE_MISMATCH", "'keys' input must be a list of object keys")
    if config.endpoint_url:
        await asyncio.to_thread(s3.check_endpoint, config.endpoint_url)

    def secret(ref: SecretRef | None) -> str | None:
        return ctx.secret(ref) if ref is not None else None

    client = s3.make_client(
        region=config.region,
        endpoint_url=config.endpoint_url,
        auth=config.auth,
        access_key_id=secret(config.access_key_id),
        secret_access_key=secret(config.secret_access_key),
        session_token=secret(config.session_token),
        timeout=120,
    )
    explicit = list(dict.fromkeys([*config.keys, *(extra or [])]))
    if explicit:
        keys = explicit[: config.max_files]
    else:
        listed = await asyncio.to_thread(
            s3.list_objects, client, config.bucket, config.prefix, config.include, config.max_files * 4
        )
        keys = [o.key for o in listed]
    max_bytes = config.max_file_mb * 2**20
    documents: list[Document] = []
    loaded: list[dict] = []
    skipped: list[str] = []
    for key in keys:
        if len(loaded) >= config.max_files:
            break
        filename = key.rsplit("/", 1)[-1]
        supported = Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS
        if not explicit and not supported:
            skipped.append(key)
            continue
        data, obj = await asyncio.to_thread(s3.get_object, client, config.bucket, key, max_bytes)
        try:
            parts = await parse_file(data, filename, config)
        except NodeError as exc:
            if explicit:
                raise
            skipped.append(key)
            await ctx.emit("s3_object_skipped", {"key": key, "reason": exc.code})
            continue
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        for part in parts:
            documents.append(
                Document(
                    id=f"s3://{config.bucket}/{key}{part.id_suffix}",
                    content=part.content,
                    source=SourceRef(
                        uri=f"s3://{config.bucket}/{key}",
                        filename=filename,
                        media_type=media_type,
                        page=part.page,
                        section=part.section,
                    ),
                    metadata={"s3_key": key, "etag": obj.etag, "last_modified": obj.last_modified,
                              "char_count": len(part.content), **part.metadata},
                    ingestion_version=S3_LOADER,
                )
            )  # fmt: skip
        loaded.append(obj.to_json())
        await ctx.emit(
            "document_loaded", {"filename": key, "parts": len(parts), "chars": sum(len(p.content) for p in parts)}
        )
    if not documents:
        where = f"s3://{config.bucket}/{config.prefix}"
        raise NodeError(
            "NO_FILES", f"No loadable files found in {where}" + (f" (skipped {len(skipped)})" if skipped else "")
        )
    return NodeResult(outputs={"documents": to_values(documents), "objects": loaded})

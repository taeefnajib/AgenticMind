"""Shared document schemas for data ingestion, RAG and evaluation nodes.

Documents travel between nodes as plain JSON objects (so they serialise into traces, stored
outputs and exported projects); these models define and validate their shape.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator


class _DocModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_DocModel):
    """Where content came from. ``uri`` is stable, e.g. ``artifact://<id>/<filename>``."""

    uri: str
    filename: str | None = None
    media_type: str | None = None
    artifact_id: str | None = None
    page: int | None = Field(default=None, ge=1, description="1-based page number, when applicable")
    section: str | None = Field(default=None, description="Heading path or logical section")


class Offsets(_DocModel):
    """Character span ``[start, end)`` of this content inside its parent document's content."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Offsets:
        if self.end < self.start:
            raise ValueError("offsets.end must be >= offsets.start")
        return self


class Document(_DocModel):
    id: str = Field(min_length=1, description="Stable, deterministic ID (used for citations and evaluation)")
    content: str
    source: SourceRef
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    parent_id: str | None = Field(default=None, description="ID of the document this was derived from")
    offsets: Offsets | None = None
    ingestion_version: str | None = Field(
        default=None, description="Node type@version chain that produced this document"
    )


class RetrievedDocument(Document):
    """A document returned by retrieval, with how and how well it matched."""

    retrieval_method: str
    score: float
    rerank_score: float | None = None


def to_value(doc: Document) -> dict[str, Any]:
    """Serialise a document to the plain JSON form used on ports."""
    return doc.model_dump(mode="json", exclude_none=True)


def to_values(docs: list[Document]) -> list[dict[str, Any]]:
    return [to_value(d) for d in docs]


def parse_document(value: Any) -> Document:
    return Document.model_validate(value)


def parse_documents(value: Any) -> list[Document]:
    """Parse a DocumentSet. Retrieval fields (score etc.) are dropped; use ``parse_retrieved`` to keep them."""
    if not isinstance(value, list):
        raise TypeError("DocumentSet must be a list")
    keys = set(Document.model_fields)
    return [Document.model_validate({k: v for k, v in item.items() if k in keys}) for item in value]


def parse_retrieved(value: Any) -> list[RetrievedDocument]:
    if not isinstance(value, list):
        raise TypeError("DocumentSet must be a list")
    return [RetrievedDocument.model_validate(v) for v in value]


def is_document(value: Any) -> bool:
    """A plain or retrieved document."""
    for model in (Document, RetrievedDocument):
        try:
            model.model_validate(value)
            return True
        except ValidationError:
            continue
    return False


def is_document_set(value: Any) -> bool:
    return isinstance(value, list) and all(is_document(v) for v in value)

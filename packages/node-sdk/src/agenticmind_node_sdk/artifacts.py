"""Read access to project files (uploads) from inside node handlers.

The runtime decides which store backs this (local disk today, S3-compatible later) and scopes it to
the run's project. Nodes only see it when they declare ``Permissions(artifacts=True)``.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class ArtifactInfo(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int = Field(ge=0)
    sha256: str
    created_at: str | None = None


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactReader(Protocol):
    """Project-scoped, read-only view of stored files."""

    def info(self, artifact_id: str) -> ArtifactInfo: ...

    def read_bytes(self, artifact_id: str) -> bytes: ...

    def list(self) -> list[ArtifactInfo]: ...

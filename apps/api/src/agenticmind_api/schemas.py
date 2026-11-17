"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    template: str | None = Field(default=None, description="Start from a bundled template")
    document: dict[str, Any] | None = Field(default=None, description="Import an existing IR document")


class ProjectSave(BaseModel):
    document: dict[str, Any]
    expected_revision: int | None = Field(
        default=None, description="Optimistic concurrency: reject the save if the project moved on"
    )


class ValidateRequest(BaseModel):
    document: dict[str, Any] | None = Field(
        default=None, description="Validate this (possibly unsaved) document instead of the saved revision"
    )


class RunCreate(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    revision: int | None = Field(default=None, description="Revision to run (default: latest)")
    targets: list[str] | None = Field(
        default=None,
        description="Run only these nodes (usually Output nodes) and everything they depend on",
    )


class ProjectSummary(BaseModel):
    id: str
    name: str
    description: str
    revision: int
    created_at: str | None
    updated_at: str | None


class ProjectOut(ProjectSummary):
    content_hash: str
    document: dict[str, Any]


class RevisionSummary(BaseModel):
    revision: int
    content_hash: str
    created_at: str | None


class RunOut(BaseModel):
    id: str
    project_id: str
    revision: int
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    error: dict[str, Any] | None
    node_status: dict[str, Any] | None
    content_hash: str
    created_at: str | None
    started_at: str | None
    finished_at: str | None

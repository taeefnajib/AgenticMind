from __future__ import annotations

import uuid
from typing import Any

from agenticmind_ir import IRLoadError, ProjectDocument, load_project, to_canonical_json
from agenticmind_runtime import validate_project
from fastapi import APIRouter, Depends, HTTPException, Response

from ..db import ProjectRevisionRow, ProjectRow
from ..deps import Services, services
from ..schemas import ProjectCreate, ProjectOut, ProjectSave, ProjectSummary, RevisionSummary, ValidateRequest
from ..store import ConflictError, iso
from ..templates import load_template

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _summary(row: ProjectRow) -> ProjectSummary:
    return ProjectSummary(
        id=row.id,
        name=row.name,
        description=row.description,
        revision=row.current_revision,
        created_at=iso(row.created_at),
        updated_at=iso(row.updated_at),
    )


def _out(row: ProjectRow, rev: ProjectRevisionRow) -> ProjectOut:
    return ProjectOut(**_summary(row).model_dump(), content_hash=rev.content_hash, document=rev.document)


def _parse_or_422(raw: dict[str, Any], project_id: str, name: str | None = None) -> ProjectDocument:
    """Parse IR, forcing the document's project ID to match the resource."""
    raw = dict(raw)
    project = dict(raw.get("project") or {})
    project["id"] = project_id
    if name is not None:
        project["name"] = name
    raw["project"] = project
    try:
        return load_project(raw)
    except IRLoadError as exc:
        raise HTTPException(
            422,
            {
                "message": str(exc),
                "diagnostics": [
                    {"code": i["code"], "severity": "error", "message": i["message"], "path": i["path"]}
                    for i in exc.issues
                ],
            },
        ) from exc


def _require(svc: Services, project_id: str) -> tuple[ProjectRow, ProjectRevisionRow]:
    row = svc.store.get_project(project_id)
    rev = svc.store.get_revision(project_id) if row else None
    if row is None or rev is None:
        raise HTTPException(404, f"Project {project_id!r} not found")
    return row, rev


@router.get("", response_model=list[ProjectSummary])
def list_projects(svc: Services = Depends(services)) -> list[ProjectSummary]:
    return [_summary(r) for r in svc.store.list_projects()]


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, svc: Services = Depends(services)) -> ProjectOut:
    project_id = f"prj_{uuid.uuid4().hex[:12]}"
    if body.document is not None and body.template is not None:
        raise HTTPException(400, "Provide either 'document' or 'template', not both")
    if body.template is not None:
        try:
            raw = load_template(body.template)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown template {body.template!r}") from exc
    elif body.document is not None:
        raw = body.document
    else:
        raw = {"ir_version": "1.0", "project": {}, "nodes": [], "edges": []}
    raw = dict(raw)
    project = dict(raw.get("project") or {})
    if body.description:
        project["description"] = body.description
    raw["project"] = project
    doc = _parse_or_422(raw, project_id, name=body.name)
    row, rev = svc.store.create_project(doc)
    return _out(row, rev)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, svc: Services = Depends(services)) -> ProjectOut:
    return _out(*_require(svc, project_id))


@router.put("/{project_id}", response_model=ProjectOut)
def save_project(project_id: str, body: ProjectSave, svc: Services = Depends(services)) -> ProjectOut:
    """Save a new revision. Drafts may be graph-invalid, but must conform to the IR schema."""
    _require(svc, project_id)
    doc = _parse_or_422(body.document, project_id)
    try:
        row, rev, _created = svc.store.save_revision(project_id, doc, body.expected_revision)
    except ConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _out(row, rev)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, svc: Services = Depends(services)) -> Response:
    if not svc.store.delete_project(project_id):
        raise HTTPException(404, f"Project {project_id!r} not found")
    return Response(status_code=204)


@router.get("/{project_id}/revisions", response_model=list[RevisionSummary])
def list_revisions(project_id: str, svc: Services = Depends(services)) -> list[RevisionSummary]:
    _require(svc, project_id)
    return [
        RevisionSummary(revision=r.revision, content_hash=r.content_hash, created_at=iso(r.created_at))
        for r in svc.store.list_revisions(project_id)
    ]


@router.get("/{project_id}/revisions/{revision}")
def get_revision(project_id: str, revision: int, svc: Services = Depends(services)) -> dict[str, Any]:
    rev = svc.store.get_revision(project_id, revision)
    if rev is None:
        raise HTTPException(404, "Revision not found")
    return {"revision": rev.revision, "content_hash": rev.content_hash, "document": rev.document}


@router.post("/{project_id}/validate")
def validate(project_id: str, body: ValidateRequest | None = None, svc: Services = Depends(services)) -> dict[str, Any]:
    _row, rev = _require(svc, project_id)
    source: dict[str, Any] = rev.document
    if body is not None and body.document is not None:
        source = dict(body.document)
        source["project"] = {**(source.get("project") or {}), "id": project_id}
    _doc, report = validate_project(
        source, svc.registry, secrets=svc.secrets, in_process_trust=svc.settings.in_process_trust
    )
    return report.to_public()


@router.get("/{project_id}/export")
def export_ir(project_id: str, revision: int | None = None, svc: Services = Depends(services)) -> Response:
    """Download the canonical IR (deterministic, diff-friendly, re-importable)."""
    row = svc.store.get_project(project_id)
    rev = svc.store.get_revision(project_id, revision) if row else None
    if row is None or rev is None:
        raise HTTPException(404, "Project or revision not found")
    body = to_canonical_json(load_project(rev.document))
    filename = f"{row.name.strip().replace(' ', '-').lower() or project_id}.agenticmind.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-AgenticMind-Revision": str(rev.revision),
            "X-AgenticMind-Content-Hash": rev.content_hash,
        },
    )

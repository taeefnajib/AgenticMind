from __future__ import annotations

import uuid
from typing import Any

from agenticmind_ir import IRLoadError, ProjectDocument, load_project, to_canonical_json
from agenticmind_runtime import validate_project
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..db import ProjectRevisionRow, ProjectRow
from ..deps import Services, services
from ..schemas import ProjectCreate, ProjectOut, ProjectSave, ProjectSummary, RevisionSummary, ValidateRequest
from ..security import require_admin
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


def _refresh_triggers(svc: Services) -> None:
    if svc.triggers is not None:
        svc.triggers.request_refresh()  # a saved revision may add, change or remove triggers


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
    _refresh_triggers(svc)
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
    _refresh_triggers(svc)
    return _out(row, rev)


@router.delete("/{project_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_project(
    project_id: str,
    confirm: str = Query(default="", description='Must be "DELETE": deleting cannot be undone'),
    svc: Services = Depends(services),
) -> Response:
    """Delete a project with all its revisions, runs, files, evaluations and vector collections (admins)."""
    if confirm != "DELETE":
        raise HTTPException(
            400,
            {"code": "CONFIRMATION_REQUIRED", "message": "Deleting a project cannot be undone: send confirm=DELETE."},
        )
    if not svc.store.delete_project(project_id):
        raise HTTPException(404, f"Project {project_id!r} not found")
    svc.artifacts.delete_project(project_id)
    # embedded vector collections created by this project (namespaced "<project>__<collection>")
    from agenticmind_vectorstores import delete_local_collections

    delete_local_collections(svc.settings.data_dir, f"{project_id}__")
    _refresh_triggers(svc)
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


def _compile(project_id: str, revision: int | None, svc: Services) -> Any:
    from agenticmind_compiler import CompileError, compile_project

    row = svc.store.get_project(project_id)
    rev = svc.store.get_revision(project_id, revision) if row else None
    if row is None or rev is None:
        raise HTTPException(404, "Project or revision not found")
    try:
        return rev, compile_project(load_project(rev.document), svc.registry, artifacts=svc.artifacts)
    except CompileError as exc:
        raise HTTPException(422, {"message": str(exc), "diagnostics": exc.diagnostics}) from exc


@router.get("/{project_id}/app/preview")
def app_preview(project_id: str, revision: int | None = None, svc: Services = Depends(services)) -> dict[str, Any]:
    """What exporting as an app would produce: endpoints, requirements, warnings and files (ADR-0016)."""
    rev, result = _compile(project_id, revision, svc)
    sizes: dict[str, int] = {}
    for path, data in result.files.items():
        top = path.split("/", 1)[0] if "/" in path else path
        sizes[top] = sizes.get(top, 0) + len(data)
    return {
        "revision": rev.revision,
        "name": result.root,
        "endpoints": [
            {
                "name": ep.name,
                "method": "POST",
                "path": ep.path,
                "summary": ep.summary,
                "auth": ep.auth,
                "rate_limit_per_minute": ep.rate_limit_per_minute,
                "request": [{"key": f.key, "type": f.value_type, "required": f.required} for f in ep.request],
                "response": [{"key": r.key, "type": r.port_type} for r in ep.response],
                "offline": ep.offline,
            }
            for ep in result.endpoints
        ],
        "requirements": result.manifest["requirements"],
        "packages": result.manifest["packages"],
        "checks": result.manifest["compiler"]["checks"],
        "warnings": result.warnings,
        "files": len(result.files),
        "bytes": sum(sizes.values()),
        "layout": dict(sorted(sizes.items())),
    }


@router.get("/{project_id}/app")
def export_app(project_id: str, revision: int | None = None, svc: Services = Depends(services)) -> Response:
    """Download the project as a standalone FastAPI app (ZIP; same IR → identical bytes)."""
    from agenticmind_compiler import to_zip

    rev, result = _compile(project_id, revision, svc)
    return Response(
        content=to_zip(result),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{result.root}.zip"',
            "X-AgenticMind-Revision": str(rev.revision),
            "X-AgenticMind-Content-Hash": rev.content_hash,
        },
    )

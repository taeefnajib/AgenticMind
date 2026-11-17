"""Project files (uploads). Bytes live in the artifact store, never in the IR."""

from __future__ import annotations

from agenticmind_node_sdk import ArtifactInfo, ArtifactNotFoundError
from agenticmind_runtime import ALLOWED_TYPES, ArtifactError
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse

from ..deps import Services, services

router = APIRouter(prefix="/api/projects/{project_id}/files", tags=["files"])

_STATUS = {"ARTIFACT_TOO_LARGE": 413, "ARTIFACT_TYPE_NOT_ALLOWED": 415, "ARTIFACT_EMPTY": 400}


def _require_project(svc: Services, project_id: str) -> None:
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")


@router.get("", response_model=list[ArtifactInfo])
def list_files(project_id: str, svc: Services = Depends(services)) -> list[ArtifactInfo]:
    _require_project(svc, project_id)
    return svc.artifacts.list(project_id)


@router.get("/types")
def allowed_types(project_id: str, svc: Services = Depends(services)) -> dict:
    return {"extensions": sorted(ALLOWED_TYPES), "max_bytes": svc.artifacts.max_bytes}


@router.post("", response_model=ArtifactInfo, status_code=201)
def upload_file(project_id: str, file: UploadFile = File(...), svc: Services = Depends(services)) -> ArtifactInfo:
    """Upload one file (multipart field ``file``). Sync handler → runs in the threadpool."""
    _require_project(svc, project_id)
    try:
        return svc.artifacts.put_stream(project_id, file.filename or "upload", file.file)
    except ArtifactError as exc:
        raise HTTPException(_STATUS.get(exc.code, 400), {"code": exc.code, "message": str(exc)}) from exc


@router.get("/{artifact_id}", response_model=ArtifactInfo)
def file_info(project_id: str, artifact_id: str, svc: Services = Depends(services)) -> ArtifactInfo:
    _require_project(svc, project_id)
    try:
        return svc.artifacts.info(project_id, artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(404, "File not found") from exc


@router.get("/{artifact_id}/content")
def download_file(project_id: str, artifact_id: str, svc: Services = Depends(services)) -> FileResponse:
    _require_project(svc, project_id)
    try:
        info = svc.artifacts.info(project_id, artifact_id)
        path = svc.artifacts.path(project_id, artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(404, "File not found") from exc
    # Always served as an attachment with nosniff, so uploaded HTML can never execute in our origin.
    return FileResponse(
        path,
        media_type=info.media_type,
        filename=info.filename,
        content_disposition_type="attachment",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.delete("/{artifact_id}", status_code=204)
def delete_file(project_id: str, artifact_id: str, svc: Services = Depends(services)) -> Response:
    _require_project(svc, project_id)
    if not svc.artifacts.delete(project_id, artifact_id):
        raise HTTPException(404, "File not found")
    return Response(status_code=204)

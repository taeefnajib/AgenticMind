"""GitHub version control endpoints (ADR-0038)."""

from __future__ import annotations

from typing import Any

from agenticmind_git import IR_FILE, GitError, GitLink
from fastapi import APIRouter, Body, Depends, HTTPException

from ..deps import Services, services

router = APIRouter(prefix="/api/projects/{project_id}/git", tags=["git"])
_STATUS = {
    "GIT_REMOTE_CHANGED": 409,
    "GIT_NOT_LINKED": 400,
    "GIT_TOKEN_MISSING": 400,
    "GIT_NOTHING_TO_PUSH": 400,
    "GIT_INVALID_PROJECT": 422,
    "GIT_REPO_INVALID": 422,
    "GIT_BRANCH_MOVED": 409,
}


def _require(svc: Services, project_id: str) -> None:
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")


def _fail(exc: GitError) -> HTTPException:
    status = _STATUS.get(exc.code) or (404 if exc.status == 404 else 502)
    return HTTPException(status, {"code": exc.code, "message": exc.message})


def _state(svc: Services, project_id: str) -> dict[str, Any]:
    link, state = svc.git.load(project_id)
    out: dict[str, Any] = {"link": link.__dict__ if link else None, "state": state}
    if link is not None:
        out["file"] = link.path(IR_FILE)
        out["repo_url"] = f"https://github.com/{link.repo}" if link.api_base == "https://api.github.com" else None
        out["token_configured"] = bool(svc.secrets.get(link.token_secret))
    return out


@router.get("")
def get_link(project_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    _require(svc, project_id)
    return _state(svc, project_id)


@router.put("")
def put_link(project_id: str, body: dict[str, Any] = Body(...), svc: Services = Depends(services)) -> dict[str, Any]:
    _require(svc, project_id)
    try:
        link = GitLink.validate(body)
    except ValueError as exc:
        field, _, message = str(exc).partition(": ")
        raise HTTPException(422, {"code": "GIT_LINK_INVALID", "field": field, "message": message or str(exc)}) from exc
    old, state = svc.git.load(project_id)
    if old is None or (old.repo, old.branch, old.folder) != (link.repo, link.branch, link.folder):
        state = {}  # a different target: the previous sync point does not apply
    svc.git.save(project_id, link, state)
    return _state(svc, project_id)


@router.delete("")
def unlink(project_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    _require(svc, project_id)
    svc.git.save(project_id, None, {})
    return _state(svc, project_id)


@router.post("/check")
async def check(project_id: str, body: dict[str, Any] = Body(...), svc: Services = Depends(services)) -> dict[str, Any]:
    _require(svc, project_id)
    try:
        link = GitLink.validate(body)
        return await svc.git.check(link)
    except ValueError as exc:
        raise HTTPException(422, {"code": "GIT_LINK_INVALID", "message": str(exc)}) from exc
    except GitError as exc:
        raise _fail(exc) from exc


@router.post("/push")
async def push(
    project_id: str, body: dict[str, Any] = Body(default={}), svc: Services = Depends(services)
) -> dict[str, Any]:
    _require(svc, project_id)
    try:
        return await svc.git.push(project_id, message=body.get("message"), force=bool(body.get("force")))
    except GitError as exc:
        raise _fail(exc) from exc


@router.post("/pull")
async def pull(project_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    _require(svc, project_id)
    try:
        return await svc.git.pull(project_id)
    except GitError as exc:
        raise _fail(exc) from exc


@router.get("/history")
async def history(project_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    _require(svc, project_id)
    try:
        return await svc.git.history(project_id)
    except GitError as exc:
        raise _fail(exc) from exc


@router.post("/restore")
async def restore(
    project_id: str, body: dict[str, Any] = Body(...), svc: Services = Depends(services)
) -> dict[str, Any]:
    _require(svc, project_id)
    sha = str(body.get("sha") or "")
    if not (7 <= len(sha) <= 40) or any(c not in "0123456789abcdef" for c in sha.lower()):
        raise HTTPException(422, {"code": "GIT_SHA_INVALID", "message": "Give a commit sha"})
    try:
        return await svc.git.pull(project_id, ref=sha)
    except GitError as exc:
        raise _fail(exc) from exc

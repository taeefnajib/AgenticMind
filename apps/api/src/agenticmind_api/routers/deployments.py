"""Deployment Center API (ADR-0020)."""

from __future__ import annotations

from typing import Any

from agenticmind_deployment import PROFILES, TARGETS
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..deploy_manager import DeployError, to_json
from ..deps import Services, services

router = APIRouter(prefix="/api", tags=["deployment"])


class PlanIn(BaseModel):
    revision: int | None = Field(default=None, description="Project revision (default: latest)")
    target: str
    profile: str = "development"
    env: dict[str, str] = Field(default_factory=dict, description="Non-secret settings")
    image: str = Field(default="", max_length=300, description="Registry/repository for guided targets")


class DeployIn(PlanIn):
    plan_hash: str = Field(description="The hash of the plan you reviewed")
    confirm: bool = Field(default=False, description="Explicit confirmation of that plan")
    rollback_of: str | None = Field(default=None, description="Set when redeploying an earlier deployment")


class UrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)


def _err(exc: DeployError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


@router.get("/deployment/targets")
def targets(svc: Services = Depends(services)) -> dict[str, Any]:
    enabled, reason = svc.deployments.local_status()
    return {
        "targets": [t.to_json() for t in TARGETS.values()],
        "profiles": [{"id": k, "summary": v} for k, v in PROFILES.items()],
        "local": {"available": enabled, "reason": reason},
    }


@router.post("/projects/{project_id}/deployments/plan")
async def plan(project_id: str, body: PlanIn, svc: Services = Depends(services)) -> dict[str, Any]:
    import asyncio

    try:
        result = await asyncio.to_thread(
            svc.deployments.plan,
            project_id,
            revision=body.revision,
            target=body.target,
            profile=body.profile,
            env=body.env,
            image=body.image,
        )
    except DeployError as exc:
        raise _err(exc) from exc
    return result.to_json()


@router.post("/projects/{project_id}/deployments", status_code=201)
async def deploy(project_id: str, body: DeployIn, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        row = await svc.deployments.create(
            project_id,
            revision=body.revision,
            target=body.target,
            profile=body.profile,
            env=body.env,
            image=body.image,
            plan_hash=body.plan_hash,
            confirm=body.confirm,
            rollback_of=body.rollback_of,
        )
    except DeployError as exc:
        raise _err(exc) from exc
    return to_json(row)


@router.get("/projects/{project_id}/deployments")
def list_deployments(project_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, "Project not found")
    return [to_json(r) for r in svc.deployments.deployments.list(project_id)]


@router.get("/deployments/{deployment_id}")
def get_deployment(deployment_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    row = svc.deployments.deployments.get(deployment_id)
    if row is None:
        raise HTTPException(404, "Deployment not found")
    return to_json(row, log_tail=svc.deployments.log_tail(deployment_id))


@router.get("/deployments/{deployment_id}/bundle")
def bundle(deployment_id: str, svc: Services = Depends(services)) -> Response:
    try:
        row, data = svc.deployments.bundle(deployment_id)
    except DeployError as exc:
        raise _err(exc) from exc
    name = row.plan.get("name", "app")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}-{row.target}-rev{row.revision}.zip"'},
    )


@router.post("/deployments/{deployment_id}/stop")
async def stop(deployment_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        return to_json(await svc.deployments.stop(deployment_id))
    except DeployError as exc:
        raise _err(exc) from exc


@router.post("/deployments/{deployment_id}/health")
async def check_health(deployment_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        return to_json(await svc.deployments.check_health(deployment_id))
    except DeployError as exc:
        raise _err(exc) from exc


@router.post("/deployments/{deployment_id}/url")
async def register_url(deployment_id: str, body: UrlIn, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        return to_json(await svc.deployments.register_url(deployment_id, body.url))
    except DeployError as exc:
        raise _err(exc) from exc

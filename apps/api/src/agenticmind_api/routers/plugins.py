"""Node packages API (ADR-0021)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from ..deps import Services, services
from ..plugin_manager import MAX_WHEEL_BYTES, PluginManagerError, to_json
from ..security import require_admin

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class ApproveIn(BaseModel):
    permissions: list[str] = Field(default_factory=list, description="Exactly the permissions it requests")
    sha256: str = Field(description="The package hash you reviewed")
    confirm: bool = False


def _err(exc: PluginManagerError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


@router.get("")
def list_plugins(svc: Services = Depends(services)) -> dict[str, Any]:
    return {
        "packages": [to_json(r) for r in svc.plugins.list()],
        # non-core packages pip-installed into the platform's own environment are not loaded (not isolated)
        "skipped_in_environment": sorted(set(svc.registry.skipped)),
    }


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
async def install_plugin(
    file: UploadFile = File(...), trust: str = Form("community"), svc: Services = Depends(services)
) -> dict[str, Any]:
    data = await file.read(MAX_WHEEL_BYTES + 1)
    try:
        row = await svc.plugins.install(file.filename or "package.whl", data, trust=trust)
    except PluginManagerError as exc:
        raise _err(exc) from exc
    return to_json(row)


@router.post("/{name}/approve", dependencies=[Depends(require_admin)])
def approve(name: str, body: ApproveIn, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        return to_json(svc.plugins.approve(name, body.permissions, sha256=body.sha256, confirm=body.confirm))
    except PluginManagerError as exc:
        raise _err(exc) from exc


@router.post("/{name}/disable", dependencies=[Depends(require_admin)])
def disable(name: str, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        return to_json(svc.plugins.set_enabled(name, False))
    except PluginManagerError as exc:
        raise _err(exc) from exc


@router.post("/{name}/enable", dependencies=[Depends(require_admin)])
def enable(name: str, svc: Services = Depends(services)) -> dict[str, Any]:
    try:
        return to_json(svc.plugins.set_enabled(name, True))
    except PluginManagerError as exc:
        raise _err(exc) from exc


@router.delete("/{name}", status_code=204, dependencies=[Depends(require_admin)])
def remove(name: str, svc: Services = Depends(services)) -> Response:
    try:
        svc.plugins.remove(name)
    except PluginManagerError as exc:
        raise _err(exc) from exc
    return Response(status_code=204)


@router.get("/{name}/wheel")
def download(name: str, svc: Services = Depends(services)) -> Response:
    try:
        filename, data = svc.plugins.wheel(name)
    except PluginManagerError as exc:
        raise _err(exc) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

"""Workspace secrets API (ADR-0026). Values are write-only: no endpoint ever returns one."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..deps import Services, services
from ..secrets_store import ChainedSecrets, SecretStoreError, secret_json
from ..security import current_user, require_admin

router = APIRouter(prefix="/api/secrets", tags=["secrets"])


class SecretIn(BaseModel):
    value: str = Field(max_length=70_000)


class StatusIn(BaseModel):
    names: list[str] = Field(max_length=200)


def _chain(svc: Services) -> ChainedSecrets:
    if not isinstance(svc.secrets, ChainedSecrets):  # pragma: no cover - always chained by create_app
        raise HTTPException(500, "Secret store not configured")
    return svc.secrets


@router.get("")
def list_secrets(request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    """Names of the secrets saved in this workspace (never their values)."""
    chain = _chain(svc)
    user = current_user(request)
    return {
        "secrets": [secret_json(r, chain) for r in chain.vault.list()],
        "can_edit": svc.settings.auth_mode == "off" or (user is not None and user.role == "admin"),
    }


@router.post("/status")
def secret_status(body: StatusIn, svc: Services = Depends(services)) -> dict[str, str | None]:
    """For each name: "environment", "workspace" or null (not set). Used by node config forms."""
    chain = _chain(svc)
    return {name: chain.source(name) for name in dict.fromkeys(body.names) if len(name) <= 128}


@router.put("/{name}")
def save_secret(
    name: str, body: SecretIn, request: Request, svc: Services = Depends(services), admin=Depends(require_admin)
) -> dict[str, Any]:
    chain = _chain(svc)
    try:
        row = chain.vault.set(name, body.value, user=admin.username if admin else None)
    except SecretStoreError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
    return secret_json(row, chain)


@router.delete("/{name}", status_code=204, dependencies=[Depends(require_admin)])
def delete_secret(name: str, svc: Services = Depends(services)) -> Response:
    if not _chain(svc).vault.delete(name):
        raise HTTPException(404, {"code": "SECRET_NOT_FOUND", "message": f"No saved secret named {name!r}"})
    return Response(status_code=204)

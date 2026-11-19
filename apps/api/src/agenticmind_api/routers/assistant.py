"""AI workflow designer endpoints (ADR-0022). Proposals are validated but never saved here."""

from __future__ import annotations

from typing import Any, Literal

from agenticmind_providers import ProviderError, get_provider
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..assistant import AssistantError, WorkflowDesigner
from ..deps import Services, services

router = APIRouter(prefix="/api", tags=["assistant"])


class HistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class DesignRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[HistoryItem] = Field(default_factory=list, max_length=50)
    document: dict[str, Any] | None = Field(
        default=None, description="The (possibly unsaved) canvas graph to change; default: the saved revision"
    )


def _designer(svc: Services) -> WorkflowDesigner:
    s = svc.settings
    try:
        provider = get_provider(s.assistant_provider)
    except ProviderError as exc:
        raise HTTPException(400, f"Unknown assistant provider {s.assistant_provider!r}") from exc
    return WorkflowDesigner(
        registry=svc.registry,
        secrets=svc.secrets,
        provider=provider,
        model=s.assistant_model,
        api_key_secret=s.assistant_api_key_secret,
        base_url=s.assistant_base_url,
        in_process_trust=s.in_process_trust,
    )


@router.get("/assistant")
def assistant_status(svc: Services = Depends(services)) -> dict[str, Any]:
    s = svc.settings
    needs_key = s.assistant_provider == "openrouter"
    return {
        "provider": s.assistant_provider,
        "model": s.assistant_model,
        "secret": s.assistant_api_key_secret if needs_key else None,
        "configured": (not needs_key) or bool(svc.secrets.get(s.assistant_api_key_secret)),
    }


@router.post("/projects/{project_id}/assistant")
async def design(project_id: str, body: DesignRequest, svc: Services = Depends(services)) -> dict[str, Any]:
    rev = svc.store.get_revision(project_id) if svc.store.get_project(project_id) else None
    if rev is None:
        raise HTTPException(404, f"Project {project_id!r} not found")
    document = dict(body.document) if body.document is not None else dict(rev.document)
    document["project"] = {**(document.get("project") or {}), "id": project_id}
    try:
        result = await _designer(svc).design(body.message, document, [h.model_dump() for h in body.history])
    except AssistantError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
    return result.to_public()

"""Workspace settings (ADR-0023): which provider and model the AI workflow designer uses."""

from __future__ import annotations

import time
from typing import Any

from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, get_provider, provider_names
from agenticmind_providers.chat import OPENROUTER_BASE_URL
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import Services, services
from ..security import current_user, require_admin
from ..workspace_settings import (
    ASSISTANT_KEY,
    KEYED_PROVIDERS,
    AssistantConfig,
    SettingsError,
    assistant_json,
    effective_assistant,
    env_assistant,
    validate_assistant,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

MAX_MODELS = 500


class AssistantIn(BaseModel):
    provider: str = Field(max_length=64)
    model: str = Field(max_length=200)
    api_key_secret: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=500)
    max_attempts: int = 3


def _validated(body: AssistantIn) -> AssistantConfig:
    try:
        return validate_assistant(body.model_dump())
    except SettingsError as exc:
        raise HTTPException(422, {"code": "SETTINGS_INVALID", "field": exc.field, "message": str(exc)}) from exc


def _key(svc: Services, cfg: AssistantConfig) -> str | None:
    return svc.secrets.get(cfg.api_key_secret) if cfg.needs_key else None


def _state(svc: Services, request: Request) -> dict[str, Any]:
    cfg, source = effective_assistant(svc.settings, svc.auth)
    row = svc.auth.get_setting(ASSISTANT_KEY)
    user = current_user(request)
    return {
        "assistant": assistant_json(cfg),
        "source": source,
        "defaults": assistant_json(env_assistant(svc.settings)),
        "configured": (not cfg.needs_key) or bool(_key(svc, cfg)),
        "providers": [{"name": n, "needs_key": n in KEYED_PROVIDERS} for n in provider_names()],
        "can_edit": svc.settings.auth_mode == "off" or (user is not None and user.role == "admin"),
        "updated_at": row.updated_at.isoformat() if row else None,
    }


@router.get("/assistant")
def get_assistant(request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    return _state(svc, request)


@router.put("/assistant")
def put_assistant(
    body: AssistantIn, request: Request, admin=Depends(require_admin), svc: Services = Depends(services)
) -> dict[str, Any]:
    cfg = _validated(body)
    svc.auth.set_setting(ASSISTANT_KEY, assistant_json(cfg), user_id=admin.id if admin else None)
    return _state(svc, request)


@router.delete("/assistant", dependencies=[Depends(require_admin)])
def reset_assistant(request: Request, svc: Services = Depends(services)) -> dict[str, Any]:
    """Forget the workspace override and use the environment defaults again."""
    svc.auth.set_setting(ASSISTANT_KEY, None, user_id=None)
    return _state(svc, request)


@router.post("/assistant/test", dependencies=[Depends(require_admin)])
async def test_assistant(body: AssistantIn, svc: Services = Depends(services)) -> dict[str, Any]:
    """Send one tiny request with these (possibly unsaved) settings to check the provider, model and key."""
    cfg = _validated(body)
    key = _key(svc, cfg)
    if cfg.needs_key and not key:
        return {"ok": False, "code": "SECRET_NOT_CONFIGURED", "message": f"The secret {cfg.api_key_secret} is not set."}
    request = ChatRequest(
        model=cfg.model,
        system="You are a connectivity check.",
        messages=[ChatMessage(role="user", content="Reply with the single word OK.")],
        temperature=0,
        max_tokens=512,  # reasoning models need room before they answer
    )
    started = time.perf_counter()
    try:
        response = await get_provider(cfg.provider).chat(request, api_key=key, base_url=cfg.base_url)
    except ProviderError as exc:
        return {"ok": False, "code": exc.code, "message": str(exc)}
    return {
        "ok": True,
        "reply": (response.text or "")[:200],
        "model": response.model or cfg.model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


@router.post("/assistant/models", dependencies=[Depends(require_admin)])
async def list_models(body: AssistantIn, svc: Services = Depends(services)) -> dict[str, Any]:
    """Models the provider offers (``GET {base}/models``, OpenAI-compatible). Mock lists its one model."""
    if body.provider == "mock":
        return {"models": [{"id": "mock-1", "name": "Mock (deterministic, offline)"}]}
    if body.provider not in KEYED_PROVIDERS:
        return {"models": []}
    import httpx

    base = (body.base_url or "").strip() or (OPENROUTER_BASE_URL if body.provider == "openrouter" else "")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(422, {"code": "SETTINGS_INVALID", "field": "base_url", "message": "Enter a base URL."})
    secret = (body.api_key_secret or "").strip()
    key = svc.secrets.get(secret) if secret else None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10)) as client:
            res = await client.get(f"{base.rstrip('/')}/models", headers=headers)
        res.raise_for_status()
        data = res.json().get("data") or []
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise HTTPException(
            502, {"code": "MODELS_UNAVAILABLE", "message": f"Could not list models: {type(exc).__name__}"}
        ) from exc
    models = [
        {"id": str(m["id"]), "name": str(m.get("name") or m["id"]), "context_length": m.get("context_length")}
        for m in data
        if isinstance(m, dict) and m.get("id")
    ]
    models.sort(key=lambda m: m["id"])
    return {"models": models[:MAX_MODELS], "total": len(models)}

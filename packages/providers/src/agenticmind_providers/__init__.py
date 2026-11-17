"""Model provider adapter layer.

Nodes talk to models only through ``ModelProvider``. Hosted and local providers plug in behind
this interface so graphs can switch providers without changing architecture.

Only the deterministic ``mock`` provider ships today. Real providers are added after verifying
their current API documentation (see docs/roadmap.md).
"""

from __future__ import annotations

from typing import Literal, Protocol

from agenticmind_node_sdk import ModelUsage
from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    system: str | None = None
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1)


class ChatResponse(BaseModel):
    text: str
    usage: ModelUsage


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelProvider(Protocol):
    name: str

    async def chat(self, request: ChatRequest, *, api_key: str | None = None) -> ChatResponse: ...


def _approx_tokens(text: str) -> int:
    return max(1, len(text.split())) if text else 0


class MockProvider:
    """Deterministic provider for development, tests and offline demos.

    Responses are clearly labelled ``[mock:<model>]`` so they are never mistaken for real output.
    """

    name = "mock"

    async def chat(self, request: ChatRequest, *, api_key: str | None = None) -> ChatResponse:
        last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
        text = f"[mock:{request.model}] {last_user}".strip()
        prompt_text = " ".join([request.system or "", *[m.content for m in request.messages]])
        return ChatResponse(
            text=text,
            usage=ModelUsage(
                provider=self.name,
                model=request.model,
                input_tokens=_approx_tokens(prompt_text),
                output_tokens=_approx_tokens(text),
                estimated_cost_usd=0.0,
            ),
        )


_PROVIDERS: dict[str, ModelProvider] = {"mock": MockProvider()}


def get_provider(name: str) -> ModelProvider:
    try:
        return _PROVIDERS[name]
    except KeyError as exc:
        raise ProviderError("PROVIDER_UNKNOWN", f"Unknown model provider {name!r}") from exc


def register_provider(provider: ModelProvider) -> None:
    _PROVIDERS[provider.name] = provider


def provider_names() -> list[str]:
    return sorted(_PROVIDERS)


__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "MockProvider",
    "ModelProvider",
    "ProviderError",
    "get_provider",
    "provider_names",
    "register_provider",
]

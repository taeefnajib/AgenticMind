"""Model provider adapter layer.

Nodes talk to models only through ``ModelProvider``. Hosted and local providers plug in behind
this interface so graphs can switch providers without changing architecture.

Providers:
* ``openrouter`` – OpenRouter's OpenAI-compatible API (default for real models; Gemini and others).
* ``openai_compatible`` – any OpenAI-style ``/chat/completions`` endpoint (``base_url`` required).
* ``mock`` – deterministic, offline; for tests and demos. Output is labelled ``[mock:<model>]``.

The request/response types already carry tool calling (used by the Agent node).
"""

from __future__ import annotations

import re
from typing import Any, Literal, Protocol

from agenticmind_node_sdk import ModelUsage
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    """A function the model may call. ``parameters`` is a JSON Schema object."""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    system: str | None = None
    temperature: float = Field(default=0.0, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1)
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_choice: Literal["auto", "none", "required"] | None = None


class ChatResponse(BaseModel):
    text: str
    usage: ModelUsage
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    model: str | None = None


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ModelProvider(Protocol):
    name: str

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse: ...


def _approx_tokens(text: str) -> int:
    return max(1, len(text.split())) if text else 0


_SOURCE_LINE = re.compile(r"^\[(\d+)\][^\n]*\n(.+?)(?:\n\n|\Z)", re.MULTILINE | re.DOTALL)


class MockProvider:
    """Deterministic provider for development, tests and offline demos.

    Responses are clearly labelled ``[mock:<model>]`` so they are never mistaken for real output.
    When the prompt contains numbered sources (``[1] ...``), the mock answers with the first sentence
    of source 1 and cites it, so citation pipelines can be exercised offline.
    """

    name = "mock"

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse:
        last_user = next((m.content or "" for m in reversed(request.messages) if m.role == "user"), "")
        match = _SOURCE_LINE.search(last_user)
        if match:
            first_sentence = re.split(r"(?<=[.!?])\s", match.group(2).strip(), maxsplit=1)[0]
            text = f"[mock:{request.model}] {first_sentence} [{match.group(1)}]"
        else:
            text = f"[mock:{request.model}] {last_user}".strip()
        prompt_text = " ".join([request.system or "", *[m.content or "" for m in request.messages]])
        return ChatResponse(
            text=text,
            usage=ModelUsage(
                provider=self.name,
                model=request.model,
                input_tokens=_approx_tokens(prompt_text),
                output_tokens=_approx_tokens(text),
                estimated_cost_usd=0.0,
            ),
            finish_reason="stop",
            model=request.model,
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


from .chat import OpenAICompatibleChat  # noqa: E402  (needs the types above)

register_provider(OpenAICompatibleChat("openrouter"))
register_provider(OpenAICompatibleChat("openai_compatible"))

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "MockProvider",
    "ModelProvider",
    "OpenAICompatibleChat",
    "ProviderError",
    "ToolCall",
    "ToolSpec",
    "get_provider",
    "provider_names",
    "register_provider",
]

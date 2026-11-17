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


def _mock_value(schema: dict[str, Any], text: str) -> Any:
    """A deterministic value satisfying a (simple) JSON Schema, built from ``text``.

    Used by the mock provider for tool arguments: strings get the text, enums their first option,
    arrays one element, objects their required properties.
    """
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {name: _mock_value(props.get(name, {}), text) for name in schema.get("required", [])}
    if kind == "array":
        return [_mock_value(schema.get("items", {}), text)]
    if kind in ("number", "integer"):
        return 0
    if kind == "boolean":
        return False
    return text


class MockProvider:
    """Deterministic provider for development, tests and offline demos.

    Responses are clearly labelled ``[mock:<model>]`` so they are never mistaken for real output.
    When the prompt contains numbered sources (``[1] ...``), the mock answers with the first sentence
    of source 1 and cites it, so citation pipelines can be exercised offline. With tools, it calls the
    tool named in the task (``"calculator: 2+3"``) or the first tool, then answers from the result.
    """

    name = "mock"

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse:
        last_user = next((m.content or "" for m in reversed(request.messages) if m.role == "user"), "")
        tool_result = self._tool_step(request, last_user)
        if tool_result is not None:
            return tool_result
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

    def _tool_step(self, request: ChatRequest, last_user: str) -> ChatResponse | None:
        """Deterministic tool use: call the tool named in the task (or the first), then answer from its result."""
        if not request.tools or request.tool_choice == "none":
            return None
        last_user_idx = (
            max(i for i, m in enumerate(request.messages) if m.role == "user")
            if any(m.role == "user" for m in request.messages)
            else -1
        )
        results = [m for m in request.messages[last_user_idx + 1 :] if m.role == "tool"]
        usage = ModelUsage(
            provider=self.name, model=request.model, input_tokens=10, output_tokens=5, estimated_cost_usd=0.0
        )
        if results:
            last = results[-1]
            text = f"[mock:{request.model}] Based on {last.name or 'the tool'}: {(last.content or '')[:200]}"
            return ChatResponse(text=text, usage=usage, finish_reason="stop", model=request.model)
        lowered = last_user.lower()
        tool = next((t for t in request.tools if t.name in lowered), request.tools[0])
        marker = f"{tool.name}:"
        text = last_user.split(marker, 1)[1].strip() if marker in last_user else last_user
        args = _mock_value(tool.parameters, text)
        if not isinstance(args, dict):
            args = {}
        call = ToolCall(id=f"mock_call_{len(request.messages)}", name=tool.name, arguments=args)
        return ChatResponse(text="", tool_calls=[call], usage=usage, finish_reason="tool_calls", model=request.model)


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

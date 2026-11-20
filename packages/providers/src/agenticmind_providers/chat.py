"""OpenAI-compatible chat completions: OpenRouter (default), Hugging Face, Ollama and any compatible server.

Verified against OpenRouter's API reference (Nov 2026): ``POST {base}/chat/completions`` with a Bearer
key; ``max_completion_tokens`` (``max_tokens`` is deprecated); OpenAI-style ``tools`` / ``tool_calls``
with JSON-string arguments; ``usage`` always includes ``prompt_tokens``, ``completion_tokens`` and, on
OpenRouter, ``cost`` in USD.

Verified Nov 2026:
- Hugging Face Inference Providers: ``https://router.huggingface.co/v1/chat/completions`` with a Bearer
  ``HF_TOKEN``; model ids may end in ``:fastest``/``:cheapest``/``:<provider>``; ``GET /v1/models`` lists them.
- Ollama: ``http://localhost:11434/v1`` (key ignored locally), vision via base64 ``image_url`` data URLs,
  tools supported; it reads ``max_tokens`` (not ``max_completion_tokens``); ``GET /v1/models``.
Images go as OpenAI content parts (``text`` + ``image_url`` with a data URL).
"""

from __future__ import annotations

import json
from typing import Any

from agenticmind_node_sdk import ModelUsage

from . import ChatRequest, ChatResponse, ProviderError, ToolCall

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
#: Default base URL per provider (None: the config must give one).
DEFAULT_BASE_URLS: dict[str, str] = {
    "openrouter": OPENROUTER_BASE_URL,
    "huggingface": HUGGINGFACE_BASE_URL,
    "ollama": OLLAMA_BASE_URL,
}
#: Providers that read the older ``max_tokens`` field.
_MAX_TOKENS_FIELD = {"ollama": "max_tokens", "huggingface": "max_tokens"}
DEFAULT_OPENROUTER_MODEL = "google/gemini-3.8-flash"
APP_TITLE = "AgenticMind"


def _message_payload(m: Any) -> dict[str, Any]:
    content: Any = m.content or ""
    if getattr(m, "images", None):
        content = [{"type": "text", "text": m.content or ""}] + [
            {"type": "image_url", "image_url": {"url": p.data_url}} for p in m.images
        ]
    payload: dict[str, Any] = {"role": m.role, "content": content}
    if m.tool_calls:
        payload["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in m.tool_calls
        ]
    if m.tool_call_id:
        payload["tool_call_id"] = m.tool_call_id
    if m.name and m.role == "tool":
        payload["name"] = m.name
    return payload


def build_body(request: ChatRequest, *, max_tokens_field: str = "max_completion_tokens") -> dict[str, Any]:
    messages = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    messages.extend(_message_payload(m) for m in request.messages)
    body: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "temperature": request.temperature,
        max_tokens_field: request.max_tokens,
    }
    if request.tools:
        body["tools"] = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in request.tools
        ]
        if request.tool_choice:
            body["tool_choice"] = request.tool_choice
    return body


def parse_response(payload: dict[str, Any], provider: str, requested_model: str) -> ChatResponse:
    if isinstance(payload.get("error"), dict):
        err = payload["error"]
        raise ProviderError("PROVIDER_ERROR", f"Provider error: {err.get('message', err)}")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("PROVIDER_BAD_RESPONSE", "Chat response has no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    calls = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except json.JSONDecodeError:
            args = {"_raw": args_raw}
        calls.append(ToolCall(id=raw.get("id") or f"call_{len(calls)}", name=fn.get("name", ""), arguments=args))
    usage = payload.get("usage") or {}
    cost = usage.get("cost")
    model = payload.get("model") or requested_model
    content = message.get("content")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ChatResponse(
        text=content or "",
        tool_calls=calls,
        finish_reason=choice.get("finish_reason"),
        model=model,
        usage=ModelUsage(
            provider=provider,
            model=model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            estimated_cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        ),
    )


class OpenAICompatibleChat:
    def __init__(self, name: str) -> None:
        self.name = name

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse:
        import httpx

        base_url = base_url or DEFAULT_BASE_URLS.get(self.name)
        if self.name == "openrouter" and not api_key:
            raise ProviderError("PROVIDER_AUTH_MISSING", "OpenRouter needs an API key (OPENROUTER_API_KEY)")
        if self.name == "huggingface" and not api_key:
            raise ProviderError("PROVIDER_AUTH_MISSING", "Hugging Face needs a token (HF_TOKEN)")
        if not base_url:
            raise ProviderError("PROVIDER_CONFIG_INVALID", f"{self.name} needs a base_url")
        headers = {"Content-Type": "application/json", "X-Title": APP_TITLE}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = base_url.rstrip("/") + "/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15)) as client:
                body = build_body(request, max_tokens_field=_MAX_TOKENS_FIELD.get(self.name, "max_completion_tokens"))
                resp = await client.post(url, json=body, headers=headers)
        except httpx.ConnectError as exc:
            hint = " Is Ollama running (ollama serve) and reachable at this base URL?" if self.name == "ollama" else ""
            raise ProviderError(
                "PROVIDER_UNAVAILABLE", f"Could not connect to {base_url}.{hint}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Chat request failed: {exc}", retryable=True) from exc
        if resp.status_code in (401, 403):
            raise ProviderError("PROVIDER_AUTH_FAILED", f"The provider rejected the API key ({resp.status_code})")
        if resp.status_code == 402:
            raise ProviderError("PROVIDER_INSUFFICIENT_CREDITS", "The provider account has insufficient credits")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"The provider returned {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise ProviderError("PROVIDER_BAD_REQUEST", f"The provider returned {resp.status_code}: {detail}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderError("PROVIDER_BAD_RESPONSE", "Chat response was not JSON") from exc
        return parse_response(payload, self.name, request.model)

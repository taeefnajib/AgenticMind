"""Token streaming (ADR-0040).

``chat_stream`` returns the same ``ChatResponse`` as ``chat`` while calling ``on_text`` with text deltas as the
model produces them. Providers that can stream implement ``stream(request, *, api_key, base_url, on_text)``;
for the others (Bedrock's binary event stream, test fakes) the full text is delivered as one delta.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from . import ChatRequest, ChatResponse, ProviderError

OnText = Callable[[str], Awaitable[None]]


async def chat_stream(
    provider: Any,
    request: ChatRequest,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    on_text: OnText | None = None,
    retries: int | None = None,
) -> ChatResponse:
    """One model call, streamed to ``on_text`` when given, with retries for transient failures (ADR-0041).

    Rate limits, 5xx responses and dropped connections are retried up to ``retries`` times (default 2, or 0
    for providers whose SDK already retries) with jittered backoff, like the official Python SDKs do, but only
    before any text reached ``on_text``, so a live preview never shows text twice.
    """
    if retries is None:
        retries = 0 if getattr(provider, "retries_internally", False) else DEFAULT_RETRIES
    streamed = False

    async def relay(delta: str) -> None:
        nonlocal streamed
        streamed = True
        await on_text(delta)  # type: ignore[misc]

    stream = getattr(provider, "stream", None)
    for attempt in range(retries + 1):
        try:
            if on_text is None or stream is None:
                response = await provider.chat(request, api_key=api_key, base_url=base_url)
                if on_text is not None and response.text:
                    await relay(response.text)
                return response
            return await stream(request, api_key=api_key, base_url=base_url, on_text=relay)
        except ProviderError as exc:
            if not exc.retryable or streamed or attempt >= retries:
                raise
            await asyncio.sleep(RETRY_BASE_SECONDS * (3**attempt) * (0.75 + random.random() / 2))
    raise AssertionError("unreachable")


DEFAULT_RETRIES = 2
RETRY_BASE_SECONDS = 0.5


async def sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """``data:`` payloads of a server-sent event stream (comments and other fields skipped)."""
    buffer: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if buffer:
                yield "\n".join(buffer)
                buffer = []
            continue
        if line.startswith(":"):
            continue  # keep-alive comment (e.g. OpenRouter's ": OPENROUTER PROCESSING")
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
    if buffer:
        yield "\n".join(buffer)


class OpenAIStreamAccumulator:
    """Rebuilds a chat completion from ``chat.completion.chunk`` deltas (content and tool-call fragments)."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] = {}
        self.model: str | None = None

    def add(self, chunk: dict[str, Any]) -> str:
        """Fold one chunk in; returns its text delta."""
        if isinstance(chunk.get("error"), dict):
            raise ProviderError("PROVIDER_ERROR", f"Provider error: {chunk['error'].get('message', chunk['error'])}")
        self.model = chunk.get("model") or self.model
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        delta_text = ""
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                delta_text += delta["content"]
            for tc in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(int(tc.get("index", 0)), {"id": None, "name": "", "arguments": ""})
                slot["id"] = tc.get("id") or slot["id"]
                fn = tc.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        if delta_text:
            self.text.append(delta_text)
        return delta_text

    def payload(self) -> dict[str, Any]:
        """The equivalent non-streaming response body (parsed by the same ``parse_response``)."""
        calls = [
            {
                "id": c["id"] or f"call_{i}",
                "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
            }
            for i, c in sorted(self.calls.items())
        ]
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.text)}
        if calls:
            message["tool_calls"] = calls
        return {
            "model": self.model,
            "usage": self.usage,
            "choices": [{"index": 0, "finish_reason": self.finish_reason, "message": message}],
        }


def loads(data: str) -> dict[str, Any] | None:
    try:
        value = json.loads(data)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None

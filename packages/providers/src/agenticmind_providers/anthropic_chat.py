"""Anthropic (Claude) through the official ``anthropic`` SDK and the Messages API (ADR-0036).

* Requests: top-level ``system``; ``messages`` with content blocks (``text``, base64 ``image``, ``tool_use``,
  ``tool_result``); every tool result of a turn goes back in **one** user message.
* Current models think adaptively and reject sampling parameters, so no ``temperature`` is sent. Thinking
  shares ``max_tokens``, so small caps are raised to a floor (unused tokens are not billed).
* Forced tool choice (``any``) returns 400 on Claude Opus 5.5, Sonnet 5.5 and Fable / Mythos 5.1+; for those,
  "required" becomes ``auto`` (the prompt and the tool list still steer the call).
* Assistant content (thinking blocks included) is kept in ``provider_state`` and sent back unchanged on the
  next request, as the API requires during tool use.
* Automatic prompt caching (top-level ``cache_control``) is on: agent loops resend the same system prompt
  and tools every turn.
* Refusals (``stop_reason == "refusal"``) raise ``PROVIDER_REFUSED``. On models that support it, the
  server-side ``fallbacks: "default"`` beta lets the API rerun a declined request on a fallback model
  (``AGENTICMIND_ANTHROPIC_FALLBACKS=0`` turns it off); ``usage.model`` reports the model that answered.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from agenticmind_node_sdk import ModelUsage

from . import ChatRequest, ChatResponse, ProviderError, ToolCall

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
MIN_MAX_TOKENS = 4096
_NO_FORCED_TOOL = re.compile(r"claude-(opus-5-5|sonnet-5-5|fable-5-[12]|mythos-5-[12])")
_FALLBACK_MODELS = re.compile(r"claude-(fable-5-[12]|opus-5-5|opus-5(?!-\d)|sonnet-5-5)")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _image_block(img: Any) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": img.media_type, "data": img.data_base64}}


def build_messages(request: ChatRequest) -> list[dict[str, Any]]:
    """Convert the provider-neutral conversation; consecutive tool results merge into one user message."""
    out: list[dict[str, Any]] = []
    for m in request.messages:
        if m.role == "system":  # mid-conversation system text: fold into a user turn (portable everywhere)
            out.append({"role": "user", "content": [{"type": "text", "text": m.content or ""}]})
        elif m.role == "user":
            blocks: list[dict[str, Any]] = [_image_block(i) for i in m.images]
            blocks.append({"type": "text", "text": m.content or ""})
            out.append({"role": "user", "content": blocks})
        elif m.role == "assistant":
            if isinstance(m.provider_state, list) and m.provider_state:
                out.append({"role": "assistant", "content": m.provider_state})  # unchanged, thinking included
                continue
            blocks = [{"type": "text", "text": m.content}] if m.content else []
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls or []
            ]
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:  # tool result
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content or "",
            }
            if (m.content or "").startswith('{"error"'):
                block["is_error"] = True
            if out and out[-1]["role"] == "user" and all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out


def build_params(request: ChatRequest) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": max(request.max_tokens, MIN_MAX_TOKENS),
        "messages": build_messages(request),
        "cache_control": {"type": "ephemeral"},
    }
    if request.system:
        params["system"] = request.system
    if request.tools and request.tool_choice != "none":
        params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                           for t in request.tools]  # fmt: skip
        if request.tool_choice == "required":
            forced = not _NO_FORCED_TOOL.search(request.model)
            params["tool_choice"] = {"type": "any" if forced else "auto"}
    if request.reasoning_effort:
        params["output_config"] = {"effort": _EFFORT.get(request.reasoning_effort, request.reasoning_effort)}
    return params


#: ``output_config.effort`` takes low | medium | high | xhigh | max; thinking cannot be disabled on current models.
_EFFORT = {"none": "low", "minimal": "low"}


def parse_message(msg: Any, requested_model: str) -> ChatResponse:
    data = msg.model_dump(mode="json", exclude_none=True) if hasattr(msg, "model_dump") else dict(msg)
    if data.get("stop_reason") == "refusal":
        details = data.get("stop_details") or {}
        raise ProviderError("PROVIDER_REFUSED", f"Claude declined the request ({details.get('category') or 'policy'})"
                            + (f": {details['explanation']}" if details.get("explanation") else ""))  # fmt: skip
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    calls = [ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
             for b in content if b.get("type") == "tool_use"]  # fmt: skip
    usage = data.get("usage") or {}
    model = data.get("model") or requested_model
    state = [b for b in content if b.get("type") != "fallback"]  # replay everything but switch markers
    return ChatResponse(
        text=text,
        tool_calls=calls,
        finish_reason=data.get("stop_reason"),
        model=model,
        provider_state=state,
        usage=ModelUsage(
            provider="anthropic",
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        ),
    )


class AnthropicChat:
    name = "anthropic"
    retries_internally = True  # the SDK retries 429/5xx/connection errors itself (max_retries=2)

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse:
        return await self._call(request, api_key, base_url, None)

    async def stream(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None, on_text: Any = None
    ) -> ChatResponse:
        """``messages.stream``: text deltas to ``on_text``, then the final message (ADR-0040)."""
        return await self._call(request, api_key, base_url, on_text)

    async def _call(
        self, request: ChatRequest, api_key: str | None, base_url: str | None, on_text: Any
    ) -> ChatResponse:
        import anthropic

        if not api_key:  # never fall back to the server's own credentials
            raise ProviderError("PROVIDER_AUTH_MISSING", "Anthropic needs an API key (ANTHROPIC_API_KEY)")
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url or None, max_retries=2, timeout=600)
        params = build_params(request)
        use_fallbacks = (
            os.environ.get("AGENTICMIND_ANTHROPIC_FALLBACKS", "1") != "0"
            and _FALLBACK_MODELS.search(request.model) is not None
            and not base_url
        )
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if use_fallbacks else {}
        api = client.beta.messages if use_fallbacks else client.messages
        try:
            if on_text is None:
                msg = await api.create(**params, **extra)
            else:
                async with api.stream(**params, **extra) as stream:
                    async for text in stream.text_stream:
                        await on_text(text)
                    msg = await stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ProviderError("PROVIDER_AUTH_FAILED", "Anthropic rejected the API key") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError("PROVIDER_AUTH_FAILED", f"Anthropic denied access: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError("PROVIDER_BAD_REQUEST", f"Unknown model or endpoint: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Anthropic rate limit reached", retryable=True) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError("PROVIDER_BAD_REQUEST", f"Anthropic: {exc.message}") from exc
        except anthropic.APIStatusError as exc:  # 5xx and 529 overloaded
            retry = exc.status_code >= 500
            code = "PROVIDER_UNAVAILABLE" if retry else "PROVIDER_BAD_REQUEST"
            message = f"Anthropic returned {exc.status_code}: {exc.message}"
            raise ProviderError(code, message, retryable=retry) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Could not reach the Anthropic API", retryable=True) from exc
        finally:
            await client.close()
        return parse_message(msg, request.model)

    async def list_models(self, api_key: str, base_url: str | None = None) -> list[dict[str, Any]]:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url or None, max_retries=1, timeout=30)
        try:
            page = await client.models.list(limit=100)
            return [{"id": m.id, "name": getattr(m, "display_name", m.id)} for m in page.data]
        finally:
            await client.close()


def _dumps(value: Any) -> str:  # pragma: no cover - debugging helper
    return json.dumps(value, ensure_ascii=False)

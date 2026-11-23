"""Amazon Bedrock through the Converse API with a Bedrock API key (ADR-0036).

``POST https://bedrock-runtime.{region}.amazonaws.com/model/{modelId}/converse`` with
``Authorization: Bearer <Bedrock API key>`` (no SigV4 / boto3 needed). ``base_url`` holds the region (e.g.
``us-east-1``) or a full endpoint URL. Content blocks: ``text``, ``image`` (``format`` + base64
``source.bytes``), ``toolUse`` and ``toolResult``; tools are ``toolSpec``s with ``inputSchema.json``; tool
choice ``auto`` / ``any``. Works with any Converse model (Claude, Nova, Llama, Mistral, ...; model ids or
inference-profile ids such as ``us.anthropic.claude-sonnet-4-6``).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from agenticmind_node_sdk import ModelUsage

from . import ChatRequest, ChatResponse, ProviderError, ToolCall

_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/gif": "gif", "image/webp": "webp"}


def endpoint(base_url: str | None) -> str:
    target = (base_url or "").strip()
    if not target:
        raise ProviderError("PROVIDER_CONFIG_INVALID", "bedrock needs base_url: the AWS region (e.g. us-east-1)")
    if target.startswith(("http://", "https://")):
        return target.rstrip("/")
    return f"https://bedrock-runtime.{target}.amazonaws.com"


def _tool_result_content(content: str | None) -> list[dict[str, Any]]:
    try:
        value = json.loads(content or "")
    except json.JSONDecodeError:
        return [{"text": content or ""}]
    return [{"json": value}] if isinstance(value, dict) else [{"text": content or ""}]


def build_body(request: ChatRequest) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for m in request.messages:
        if m.role in ("user", "system"):
            blocks: list[dict[str, Any]] = []
            for img in m.images:
                fmt = _FORMATS.get(img.media_type)
                if fmt is None:
                    raise ProviderError("PROVIDER_BAD_REQUEST", f"Bedrock does not accept {img.media_type} images")
                blocks.append({"image": {"format": fmt, "source": {"bytes": img.data_base64}}})
            blocks.append({"text": m.content or " "})
            messages.append({"role": "user", "content": blocks})
        elif m.role == "assistant":
            blocks = [{"text": m.content}] if m.content else []
            blocks += [
                {"toolUse": {"toolUseId": c.id, "name": c.name, "input": c.arguments}} for c in m.tool_calls or []
            ]
            messages.append({"role": "assistant", "content": blocks or [{"text": " "}]})
        else:
            result = {"toolResult": {"toolUseId": m.tool_call_id or "", "content": _tool_result_content(m.content)}}
            if (m.content or "").startswith('{"error"'):
                result["toolResult"]["status"] = "error"
            if messages and messages[-1]["role"] == "user" and all("toolResult" in b for b in messages[-1]["content"]):
                messages[-1]["content"].append(result)
            else:
                messages.append({"role": "user", "content": [result]})
    body: dict[str, Any] = {
        "messages": messages,
        "inferenceConfig": {"maxTokens": request.max_tokens, "temperature": request.temperature},
    }
    if request.system:
        body["system"] = [{"text": request.system}]
    if request.tools and request.tool_choice != "none":
        body["toolConfig"] = {
            "tools": [{"toolSpec": {"name": t.name, "description": t.description or t.name,
                                    "inputSchema": {"json": t.parameters}}} for t in request.tools],
            "toolChoice": {"any": {}} if request.tool_choice == "required" else {"auto": {}},
        }  # fmt: skip
    return body


def parse_response(payload: dict[str, Any], requested_model: str) -> ChatResponse:
    message = (payload.get("output") or {}).get("message") or {}
    content = message.get("content") or []
    text = "".join(b.get("text", "") for b in content if "text" in b)
    uses = [b["toolUse"] for b in content if "toolUse" in b]
    calls = [ToolCall(id=u["toolUseId"], name=u["name"], arguments=u.get("input") or {}) for u in uses]
    stop = payload.get("stopReason")
    if stop in ("guardrail_intervened", "content_filtered") and not text and not calls:
        raise ProviderError("PROVIDER_REFUSED", f"Bedrock blocked the response ({stop})")
    usage = payload.get("usage") or {}
    return ChatResponse(
        text=text,
        tool_calls=calls,
        finish_reason=stop,
        model=requested_model,
        usage=ModelUsage(
            provider="bedrock",
            model=requested_model,
            input_tokens=int(usage.get("inputTokens") or 0),
            output_tokens=int(usage.get("outputTokens") or 0),
        ),
    )


class BedrockChat:
    name = "bedrock"

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse:
        import httpx

        if not api_key:
            raise ProviderError("PROVIDER_AUTH_MISSING", "Bedrock needs an API key (AWS_BEARER_TOKEN_BEDROCK)")
        url = f"{endpoint(base_url)}/model/{quote(request.model, safe='')}/converse"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
                resp = await client.post(url, json=build_body(request), headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Could not reach Bedrock: {type(exc).__name__}",
                                retryable=True) from exc  # fmt: skip
        if resp.status_code in (401, 403):
            raise ProviderError("PROVIDER_AUTH_FAILED", f"Bedrock rejected the request ({resp.status_code})")
        if resp.status_code in (429, 503) or resp.status_code >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Bedrock returned {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError("PROVIDER_BAD_REQUEST", f"Bedrock returned {resp.status_code}: {resp.text[:300]}")
        return parse_response(resp.json(), request.model)

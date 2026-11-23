"""Google Gemini through the Gemini API's ``generateContent`` (ADR-0036).

``POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`` with
``x-goog-api-key``. Roles are ``user`` / ``model``; images are ``inlineData`` (base64); tools are
``functionDeclarations`` with ``parametersJsonSchema`` (plain JSON Schema). Function calls carry an ``id``
that the matching ``functionResponse`` echoes, and thinking models return ``thoughtSignature``s that must
come back on later turns: the model's parts are therefore kept in ``provider_state`` and replayed
unchanged. Thinking shares ``maxOutputTokens``, so small caps are raised to a floor.
"""

from __future__ import annotations

import json
from typing import Any

from agenticmind_node_sdk import ModelUsage

from . import ChatRequest, ChatResponse, ProviderError, ToolCall

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
MIN_OUTPUT_TOKENS = 4096


def _tool_response(content: str | None) -> dict[str, Any]:
    try:
        value = json.loads(content or "null")
    except json.JSONDecodeError:
        value = content
    return value if isinstance(value, dict) else {"result": value}


def build_body(request: ChatRequest) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    names: dict[str, str] = {}
    for m in request.messages:
        if m.role in ("user", "system"):
            parts: list[dict[str, Any]] = [{"inlineData": {"mimeType": i.media_type, "data": i.data_base64}}
                                           for i in m.images]  # fmt: skip
            parts.append({"text": m.content or ""})
            contents.append({"role": "user", "parts": parts})
        elif m.role == "assistant":
            for c in m.tool_calls or []:
                names[c.id] = c.name
            if isinstance(m.provider_state, list) and m.provider_state:
                contents.append({"role": "model", "parts": m.provider_state})  # thought signatures preserved
                continue
            parts = [{"text": m.content}] if m.content else []
            parts += [{"functionCall": {"id": c.id, "name": c.name, "args": c.arguments}} for c in m.tool_calls or []]
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        else:
            part = {"functionResponse": {"id": m.tool_call_id, "name": m.name or names.get(m.tool_call_id or "", ""),
                                         "response": _tool_response(m.content)}}  # fmt: skip
            if (
                contents
                and contents[-1]["role"] == "user"
                and all("functionResponse" in p for p in contents[-1]["parts"])
            ):
                contents[-1]["parts"].append(part)  # all results of one turn together
            else:
                contents.append({"role": "user", "parts": [part]})
    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": request.temperature,
            "maxOutputTokens": max(request.max_tokens, MIN_OUTPUT_TOKENS),
        },  # fmt: skip
    }
    if request.system:
        body["systemInstruction"] = {"parts": [{"text": request.system}]}
    if request.tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t.name, "description": t.description or t.name, "parametersJsonSchema": t.parameters}
            for t in request.tools]}]  # fmt: skip
        mode = {"required": "ANY", "none": "NONE"}.get(request.tool_choice or "", "AUTO")
        body["toolConfig"] = {"functionCallingConfig": {"mode": mode}}
    return body


def parse_response(payload: dict[str, Any], requested_model: str) -> ChatResponse:
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason")
        raise ProviderError("PROVIDER_REFUSED" if reason else "PROVIDER_BAD_RESPONSE",
                            f"Gemini returned no answer{f' (blocked: {reason})' if reason else ''}")  # fmt: skip
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
    calls = []
    for i, p in enumerate(parts):
        fc = p.get("functionCall")
        if fc:
            calls.append(
                ToolCall(id=fc.get("id") or f"gemini_call_{i}", name=fc.get("name", ""), arguments=fc.get("args") or {})
            )
    finish = cand.get("finishReason")
    if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST") and not text and not calls:
        raise ProviderError("PROVIDER_REFUSED", f"Gemini declined the request ({finish})")
    usage = payload.get("usageMetadata") or {}
    model = payload.get("modelVersion") or requested_model
    return ChatResponse(
        text=text,
        tool_calls=calls,
        finish_reason=finish,
        model=model,
        provider_state=parts,
        usage=ModelUsage(
            provider="gemini",
            model=model,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
        ),
    )


class GeminiChat:
    name = "gemini"

    async def chat(
        self, request: ChatRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> ChatResponse:
        import httpx

        if not api_key:
            raise ProviderError("PROVIDER_AUTH_MISSING", "Gemini needs an API key (GEMINI_API_KEY)")
        model = request.model.removeprefix("models/")
        url = f"{(base_url or GEMINI_BASE_URL).rstrip('/')}/models/{model}:generateContent"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15)) as client:
                resp = await client.post(url, json=build_body(request), headers={"x-goog-api-key": api_key})
        except httpx.HTTPError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Could not reach the Gemini API: {type(exc).__name__}",
                                retryable=True) from exc  # fmt: skip
        if resp.status_code in (401, 403):
            raise ProviderError("PROVIDER_AUTH_FAILED", "Gemini rejected the API key")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ProviderError("PROVIDER_UNAVAILABLE", f"Gemini returned {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise ProviderError("PROVIDER_BAD_REQUEST", f"Gemini returned {resp.status_code}: {resp.text[:300]}")
        return parse_response(resp.json(), request.model)

    async def list_models(self, api_key: str, base_url: str | None = None) -> list[dict[str, Any]]:
        import httpx

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{(base_url or GEMINI_BASE_URL).rstrip('/')}/models",
                                    headers={"x-goog-api-key": api_key}, params={"pageSize": 1000})  # fmt: skip
        resp.raise_for_status()
        return [{"id": m["name"].removeprefix("models/"), "name": m.get("displayName", m["name"])}
                for m in resp.json().get("models", [])
                if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])]  # fmt: skip

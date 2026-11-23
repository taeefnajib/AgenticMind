"""Native providers (ADR-0036): request shapes, response parsing and HTTP round trips with mock transports."""

from __future__ import annotations

import json

import httpx
import pytest
from agenticmind_providers import (
    KEYED_CHAT_PROVIDERS,
    PROVIDERS,
    ChatMessage,
    ChatRequest,
    ImagePart,
    ProviderError,
    ToolCall,
    ToolSpec,
    check_chat_provider,
    get_provider,
    list_models,
)
from agenticmind_providers import anthropic_chat as ac
from agenticmind_providers import bedrock_chat as bc
from agenticmind_providers import gemini_chat as gc

KEY = "fixture-provider-key"
CALC = ToolSpec(
    name="calculator",
    description="Arithmetic",
    parameters={"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
)
PNG = ImagePart(media_type="image/png", data_base64="iVBORw0KGgo=")


def tool_conversation() -> ChatRequest:
    return ChatRequest(
        model="m",
        system="Be exact.",
        max_tokens=500,
        temperature=0.2,
        tools=[CALC],
        tool_choice="required",
        messages=[
            ChatMessage(role="user", content="What is 6*7 and 2+2?", images=[PNG]),
            ChatMessage(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="calculator", arguments={"expression": "6*7"}),
                    ToolCall(id="c2", name="calculator", arguments={"expression": "2+2"}),
                ],
            ),
            ChatMessage(role="tool", tool_call_id="c1", name="calculator", content='{"result": 42}'),
            ChatMessage(role="tool", tool_call_id="c2", name="calculator", content='{"error": "busy"}'),
        ],
    )


def install(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


# ---- registry & validation -------------------------------------------------------------------
def test_registry_and_validation():
    for name in (
        "openai",
        "anthropic",
        "gemini",
        "azure_openai",
        "bedrock",
        "mistral",
        "groq",
        "deepseek",
        "together",
        "xai",
    ):
        assert get_provider(name).name == name and name in PROVIDERS and name in KEYED_CHAT_PROVIDERS
    assert KEYED_CHAT_PROVIDERS["anthropic"] == "ANTHROPIC_API_KEY"
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        check_chat_provider("anthropic", None, None)
    with pytest.raises(ValueError, match="resource endpoint"):
        check_chat_provider("azure_openai", object(), None)
    with pytest.raises(ValueError, match="AWS region"):
        check_chat_provider("bedrock", object(), None)
    check_chat_provider("ollama", None, None)


# ---- OpenAI-compatible presets and Azure -----------------------------------------------------
@pytest.mark.parametrize(
    "provider,url,field,auth",
    [
        ("openai", "https://api.openai.com/v1/chat/completions", "max_completion_tokens", "authorization"),
        ("mistral", "https://api.mistral.ai/v1/chat/completions", "max_tokens", "authorization"),
        ("groq", "https://api.groq.com/openai/v1/chat/completions", "max_completion_tokens", "authorization"),
        ("deepseek", "https://api.deepseek.com/chat/completions", "max_tokens", "authorization"),
        ("together", "https://api.together.ai/v1/chat/completions", "max_tokens", "authorization"),
        ("xai", "https://api.x.ai/v1/chat/completions", "max_tokens", "authorization"),
    ],
)
async def test_openai_compatible_presets(monkeypatch, provider, url, field, auth):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), body=json.loads(request.content), auth=request.headers.get(auth))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    install(monkeypatch, handler)
    r = await get_provider(provider).chat(
        ChatRequest(model="x", messages=[ChatMessage(role="user", content="hi")], max_tokens=50), api_key=KEY
    )
    assert seen["url"] == url and seen["body"][field] == 50 and seen["auth"] == f"Bearer {KEY}"
    assert r.text == "hi" and r.usage.provider == provider


async def test_azure_openai_endpoint_and_api_key_header(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(
            url=str(request.url), key=request.headers.get("api-key"), bearer=request.headers.get("authorization")
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}})

    install(monkeypatch, handler)
    await get_provider("azure_openai").chat(
        ChatRequest(model="my-deployment", messages=[]), api_key=KEY, base_url="https://acme.openai.azure.com"
    )
    assert seen["url"] == "https://acme.openai.azure.com/openai/v1/chat/completions"
    assert seen["key"] == KEY and seen["bearer"] is None
    with pytest.raises(ProviderError) as exc:
        await get_provider("openai").chat(ChatRequest(model="x", messages=[]))
    assert exc.value.code == "PROVIDER_AUTH_MISSING" and "OPENAI_API_KEY" in str(exc.value)


# ---- Anthropic -------------------------------------------------------------------------------
def test_anthropic_request_shape():
    req = tool_conversation()
    req.model = "claude-haiku-4-5"
    p = ac.build_params(req)
    assert p["system"] == "Be exact." and p["max_tokens"] == ac.MIN_MAX_TOKENS and "temperature" not in p
    assert p["cache_control"] == {"type": "ephemeral"} and p["tool_choice"] == {"type": "any"}
    assert p["tools"][0] == {"name": "calculator", "description": "Arithmetic", "input_schema": CALC.parameters}
    user, assistant, results = p["messages"]
    assert user["content"][0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
    }
    assert [b["type"] for b in assistant["content"]] == ["tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in results["content"]] == ["c1", "c2"]  # one user message for both results
    assert results["content"][1]["is_error"] is True
    req.model = "claude-opus-5-5"
    assert ac.build_params(req)["tool_choice"] == {"type": "auto"}  # forced tool choice 400s on this model


def test_anthropic_replays_provider_state_and_parses():
    msg = {
        "model": "claude-opus-5-5",
        "stop_reason": "tool_use",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Let me compute."},
            {"type": "tool_use", "id": "t1", "name": "calculator", "input": {"expression": "6*7"}},
        ],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5},
    }
    r = ac.parse_message(msg, "claude-opus-5-5")
    assert r.text == "Let me compute." and r.tool_calls[0].arguments == {"expression": "6*7"}
    assert r.usage.input_tokens == 100 and r.usage.output_tokens == 5 and r.usage.estimated_cost_usd is None
    follow = ChatRequest(
        model="claude-opus-5-5",
        messages=[
            ChatMessage(role="user", content="q"),
            r.assistant_message(),
            ChatMessage(role="tool", tool_call_id="t1", content="42"),
        ],
    )
    replayed = ac.build_params(follow)["messages"][1]["content"]
    assert replayed[0] == {"type": "thinking", "thinking": "", "signature": "sig"}  # thinking sent back unchanged
    with pytest.raises(ProviderError) as exc:
        ac.parse_message({"stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": []}, "m")
    assert exc.value.code == "PROVIDER_REFUSED" and "cyber" in str(exc.value)


async def test_anthropic_through_the_sdk(monkeypatch):
    import anthropic
    import httpx2

    seen = {}

    def handler(request):
        seen.update(
            url=str(request.url),
            auth=request.headers.get("authorization"),
            body=json.loads(request.content),
            beta=request.headers.get("anthropic-beta"),
            version=request.headers.get("anthropic-version"),
        )
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Hello!"}],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    real = anthropic.AsyncAnthropic
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **kw: real(**kw, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))),
    )
    r = await get_provider("anthropic").chat(
        ChatRequest(model="claude-opus-5-5", messages=[ChatMessage(role="user", content="hi")]), api_key=KEY
    )
    assert r.text == "Hello!" and r.usage.provider == "anthropic" and r.usage.input_tokens == 4
    assert seen["url"].startswith("https://api.anthropic.com/v1/messages") and seen["auth"] == f"Bearer {KEY}"
    assert seen["version"] == "2026-10-21" and "temperature" not in seen["body"]  # sampling params 400 there
    assert seen["body"]["fallbacks"] == "default" and ac.FALLBACK_BETA in (seen["beta"] or "")
    monkeypatch.setenv("AGENTICMIND_ANTHROPIC_FALLBACKS", "0")
    await get_provider("anthropic").chat(ChatRequest(model="claude-opus-5-5", messages=[]), api_key=KEY)
    assert "fallbacks" not in seen["body"]
    with pytest.raises(ProviderError) as exc:
        await get_provider("anthropic").chat(ChatRequest(model="m", messages=[]))
    assert exc.value.code == "PROVIDER_AUTH_MISSING"  # never the server's own credentials


# ---- Gemini ----------------------------------------------------------------------------------
def test_gemini_request_shape():
    body = gc.build_body(tool_conversation())
    assert body["systemInstruction"] == {"parts": [{"text": "Be exact."}]}
    assert body["generationConfig"] == {"temperature": 0.2, "maxOutputTokens": gc.MIN_OUTPUT_TOKENS}
    decl = body["tools"][0]["functionDeclarations"][0]
    assert (
        decl["parametersJsonSchema"] == CALC.parameters and body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    )
    user, model, results = body["contents"]
    assert user["parts"][0] == {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    assert model["role"] == "model" and model["parts"][0]["functionCall"]["id"] == "c1"
    assert [p["functionResponse"]["id"] for p in results["parts"]] == ["c1", "c2"]
    assert results["parts"][0]["functionResponse"]["response"] == {"result": 42}


def test_gemini_parse_and_thought_signatures():
    payload = {
        "modelVersion": "gemini-3.8-flash",
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "thinking...", "thought": True},
                        {
                            "functionCall": {"id": "f1", "name": "calculator", "args": {"expression": "1+1"}},
                            "thoughtSignature": "opaque",
                        },
                    ],
                },
            }
        ],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 4, "thoughtsTokenCount": 30},
    }
    r = gc.parse_response(payload, "gemini-3.8-flash")
    assert r.text == "" and r.tool_calls[0].id == "f1" and r.usage.output_tokens == 34
    follow = ChatRequest(model="g", messages=[ChatMessage(role="user", content="q"), r.assistant_message()])
    assert gc.build_body(follow)["contents"][1]["parts"][1]["thoughtSignature"] == "opaque"
    with pytest.raises(ProviderError) as exc:
        gc.parse_response({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}, "g")
    assert exc.value.code == "PROVIDER_REFUSED"


async def test_gemini_http(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), key=request.headers.get("x-goog-api-key"))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.8-flash",
                            "displayName": "Gemini 3.8 Flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {"name": "models/text-embedding", "supportedGenerationMethods": ["embedContent"]},
                    ]
                },
            )
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "hi"}]}}], "usageMetadata": {}})

    install(monkeypatch, handler)
    r = await get_provider("gemini").chat(ChatRequest(model="gemini-3.8-flash", messages=[]), api_key=KEY)
    assert r.text == "hi" and seen["key"] == KEY
    assert seen["url"] == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
    assert await list_models("gemini", KEY) == [{"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"}]


# ---- Bedrock ---------------------------------------------------------------------------------
def test_bedrock_request_shape_and_parse():
    body = bc.build_body(tool_conversation())
    assert body["system"] == [{"text": "Be exact."}] and body["inferenceConfig"] == {
        "maxTokens": 500,
        "temperature": 0.2,
    }
    assert body["toolConfig"]["toolChoice"] == {"any": {}}
    assert body["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": CALC.parameters}
    user, assistant, results = body["messages"]
    assert user["content"][0] == {"image": {"format": "png", "source": {"bytes": "iVBORw0KGgo="}}}
    assert assistant["content"][0]["toolUse"] == {
        "toolUseId": "c1",
        "name": "calculator",
        "input": {"expression": "6*7"},
    }
    assert results["content"][0]["toolResult"]["content"] == [{"json": {"result": 42}}]
    assert results["content"][1]["toolResult"]["status"] == "error"
    r = bc.parse_response(
        {
            "output": {
                "message": {
                    "content": [
                        {"text": "ok"},
                        {"toolUse": {"toolUseId": "u1", "name": "calculator", "input": {"expression": "2"}}},
                    ]
                }
            },
            "stopReason": "tool_use",
            "usage": {"inputTokens": 9, "outputTokens": 3},
        },
        "us.anthropic.claude-sonnet-4-6",
    )
    assert r.text == "ok" and r.tool_calls[0].id == "u1" and r.usage.input_tokens == 9
    assert bc.endpoint("eu-west-1") == "https://bedrock-runtime.eu-west-1.amazonaws.com"


async def test_bedrock_http(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "output": {"message": {"content": [{"text": "hi"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 1},
            },
        )

    install(monkeypatch, handler)
    await get_provider("bedrock").chat(
        ChatRequest(model="us.anthropic.claude-sonnet-4-6:0", messages=[]), api_key=KEY, base_url="us-east-1"
    )
    assert (
        seen["url"]
        == "https://bedrock-runtime.us-east-1.amazonaws.com/model/us.anthropic.claude-sonnet-4-6%3A0/converse"
    )
    assert seen["auth"] == f"Bearer {KEY}"
    with pytest.raises(ProviderError) as exc:
        await get_provider("bedrock").chat(ChatRequest(model="m", messages=[]), api_key=KEY)
    assert exc.value.code == "PROVIDER_CONFIG_INVALID"

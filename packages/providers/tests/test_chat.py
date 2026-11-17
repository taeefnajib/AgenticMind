import json

import httpx
import pytest
from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, ToolCall, ToolSpec, get_provider
from agenticmind_providers.chat import build_body, parse_response


def install(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def ok_payload(**message):
    return {
        "model": "google/gemini-3.8-flash",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 7, "total_tokens": 49, "cost": 0.000123},
    }


async def test_openrouter_request_and_response(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["title"] = request.headers.get("x-title")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_payload(content="Employees get 25 days [1]."))

    install(monkeypatch, handler)
    resp = await get_provider("openrouter").chat(
        ChatRequest(
            model="google/gemini-3.8-flash",
            system="Be precise.",
            messages=[ChatMessage(role="user", content="How much leave?")],
            temperature=0.1,
            max_tokens=300,
        ),
        api_key="sk-or-test",
    )
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-or-test" and seen["title"] == "AgenticMind"
    assert seen["body"] == {
        "model": "google/gemini-3.8-flash",
        "messages": [{"role": "system", "content": "Be precise."}, {"role": "user", "content": "How much leave?"}],
        "temperature": 0.1,
        "max_completion_tokens": 300,
    }
    assert resp.text == "Employees get 25 days [1]."
    assert resp.usage.input_tokens == 42 and resp.usage.output_tokens == 7
    assert resp.usage.estimated_cost_usd == pytest.approx(0.000123)
    assert resp.usage.provider == "openrouter" and resp.finish_reason == "stop"


def test_tool_calling_round_trip_format():
    req = ChatRequest(
        model="m",
        messages=[
            ChatMessage(role="user", content="weather?"),
            ChatMessage(
                role="assistant", content="", tool_calls=[ToolCall(id="c1", name="weather", arguments={"city": "Oslo"})]
            ),
            ChatMessage(role="tool", content='{"temp": 3}', tool_call_id="c1", name="weather"),
        ],
        tools=[
            ToolSpec(
                name="weather",
                description="Get weather",
                parameters={"type": "object", "properties": {"city": {"type": "string"}}},
            )
        ],
        tool_choice="auto",
    )
    body = build_body(req)
    assert body["tools"][0] == {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
    assert body["tool_choice"] == "auto"
    assert body["messages"][1]["tool_calls"][0]["function"] == {"name": "weather", "arguments": '{"city": "Oslo"}'}
    assert body["messages"][2] == {"role": "tool", "content": '{"temp": 3}', "tool_call_id": "c1", "name": "weather"}

    resp = parse_response(
        ok_payload(
            content=None,
            tool_calls=[
                {"id": "c2", "type": "function", "function": {"name": "weather", "arguments": '{"city": "Bergen"}'}}
            ],
        ),
        "openrouter",
        "m",
    )
    assert resp.text == "" and resp.tool_calls == [ToolCall(id="c2", name="weather", arguments={"city": "Bergen"})]


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (401, "PROVIDER_AUTH_FAILED", False),
        (402, "PROVIDER_INSUFFICIENT_CREDITS", False),
        (429, "PROVIDER_UNAVAILABLE", True),
        (503, "PROVIDER_UNAVAILABLE", True),
        (400, "PROVIDER_BAD_REQUEST", False),
    ],
)
async def test_http_errors_are_classified(monkeypatch, status, code, retryable):
    install(monkeypatch, lambda request: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(ProviderError) as exc:
        await get_provider("openrouter").chat(
            ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")]), api_key="k"
        )
    assert exc.value.code == code and exc.value.retryable is retryable


async def test_missing_key_and_base_url():
    with pytest.raises(ProviderError) as exc:
        await get_provider("openrouter").chat(ChatRequest(model="m", messages=[]))
    assert exc.value.code == "PROVIDER_AUTH_MISSING"
    with pytest.raises(ProviderError) as exc:
        await get_provider("openai_compatible").chat(ChatRequest(model="m", messages=[]), api_key="k")
    assert exc.value.code == "PROVIDER_CONFIG_INVALID"


def test_error_payload_with_200_status():
    with pytest.raises(ProviderError) as exc:
        parse_response({"error": {"message": "model not found"}}, "openrouter", "m")
    assert "model not found" in str(exc.value)


async def test_mock_cites_first_source():
    prompt = "Sources:\n[1] handbook.md · Leave\nStaff get 25 days. More text.\n\n[2] other\nx\n\nQuestion: leave?"
    resp = await get_provider("mock").chat(
        ChatRequest(model="mock-1", messages=[ChatMessage(role="user", content=prompt)])
    )
    assert resp.text == "[mock:mock-1] Staff get 25 days. [1]"

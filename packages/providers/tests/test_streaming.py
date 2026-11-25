"""Token streaming (ADR-0040): SSE parsing, tool-call reassembly, per-provider streams and the fallback."""

from __future__ import annotations

import json

import httpx
from agenticmind_providers import ChatMessage, ChatRequest, ToolSpec, chat_stream, get_provider
from agenticmind_providers.streaming import OpenAIStreamAccumulator

KEY = "fixture-provider-key"
REQ = ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")], max_tokens=50)


def sse(*events: dict | str) -> bytes:
    out = [": keep-alive comment\n\n"]
    for e in events:
        out.append(f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n")
    return "".join(out).encode()


REAL_CLIENT = httpx.AsyncClient


def install(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


async def collect(provider, request=REQ, **kw):
    deltas: list[str] = []

    async def on_text(t):
        deltas.append(t)

    resp = await chat_stream(provider, request, api_key=KEY, on_text=on_text, **kw)
    return resp, deltas


def test_accumulator_rebuilds_tool_calls_from_fragments():
    acc = OpenAIStreamAccumulator()
    acc.add(
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "calc", "arguments": '{"ex'}}]}}
            ]
        }
    )
    acc.add({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'pression": "6*7"}'}}]}}]})
    acc.add(
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4},
        }
    )
    msg = acc.payload()["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"] == {"name": "calc", "arguments": '{"expression": "6*7"}'}
    assert acc.finish_reason == "tool_calls" and acc.usage["prompt_tokens"] == 9


async def test_openai_compatible_stream(monkeypatch):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        body = sse(
            {"model": "m1", "choices": [{"delta": {"content": "Hel"}}]},
            {"choices": [{"delta": {"content": "lo!"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0.0001}},
            "[DONE]",
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    install(monkeypatch, handler)
    resp, deltas = await collect(get_provider("openrouter"))
    assert deltas == ["Hel", "lo!"] and resp.text == "Hello!" and resp.finish_reason == "stop"
    assert resp.usage.input_tokens == 3 and resp.usage.estimated_cost_usd == 0.0001 and resp.model == "m1"
    assert seen["body"]["stream"] is True and seen["body"]["stream_options"] == {"include_usage": True}

    tool_req = REQ.model_copy(update={"tools": [ToolSpec(name="calc", parameters={"type": "object"})]})

    def tools(request):
        body = sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "id": "t1", "function": {"name": "calc", "arguments": "{}"}}]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        )
        return httpx.Response(200, content=body)

    install(monkeypatch, tools)
    resp, deltas = await collect(get_provider("openai"), tool_req)
    assert deltas == [] and resp.tool_calls[0].name == "calc" and resp.tool_calls[0].id == "t1"


async def test_gemini_stream(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        body = sse(
            {"candidates": [{"content": {"parts": [{"text": "think", "thought": True}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "Bon"}]}}]},
            {
                "candidates": [{"content": {"parts": [{"text": "jour"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "thoughtsTokenCount": 7},
                "modelVersion": "gemini-3.8-flash",
            },
        )
        return httpx.Response(200, content=body)

    install(monkeypatch, handler)
    resp, deltas = await collect(get_provider("gemini"), REQ.model_copy(update={"model": "gemini-3.8-flash"}))
    assert seen["url"].endswith(":streamGenerateContent?alt=sse")
    assert deltas == ["Bon", "jour"] and resp.text == "Bonjour"  # thoughts are not streamed
    assert resp.usage.output_tokens == 9 and len(resp.provider_state) == 3  # all parts kept for replay


async def test_anthropic_stream_through_the_sdk(monkeypatch):
    import anthropic
    import httpx2

    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        body = "".join(
            [
                ev(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_1",
                            "type": "message",
                            "role": "assistant",
                            "model": "claude-opus-5-5",
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 4, "output_tokens": 1},
                        },
                    },
                ),
                ev(
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                ),
                ev(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi "}},
                ),
                ev(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "there"}},
                ),
                ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ev(
                    "message_delta",
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
                ),
                ev("message_stop", {"type": "message_stop"}),
            ]
        )
        return httpx2.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    real = anthropic.AsyncAnthropic
    monkeypatch.setattr(
        anthropic,
        "AsyncAnthropic",
        lambda **kw: real(**kw, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler))),
    )
    monkeypatch.setenv("AGENTICMIND_ANTHROPIC_FALLBACKS", "0")
    resp, deltas = await collect(get_provider("anthropic"), REQ.model_copy(update={"model": "claude-opus-5-5"}))
    assert deltas == ["Hi ", "there"] and resp.text == "Hi there" and resp.finish_reason == "end_turn"
    assert resp.usage.input_tokens == 4 and resp.usage.output_tokens == 3


async def test_fallback_and_mock():
    class NoStream:
        async def chat(self, request, *, api_key=None, base_url=None):
            from agenticmind_node_sdk import ModelUsage
            from agenticmind_providers import ChatResponse

            return ChatResponse(text="whole answer", usage=ModelUsage(provider="x", model="m"))

    resp, deltas = await collect(NoStream())
    assert deltas == ["whole answer"] and resp.text == "whole answer"
    resp, deltas = await collect(get_provider("mock"))
    assert "".join(deltas) == resp.text and len(deltas) > 1  # word by word
    plain = await chat_stream(get_provider("mock"), REQ)  # no listener: a normal call
    assert plain.text == resp.text


async def test_transient_failures_are_retried_before_any_text(monkeypatch):
    from agenticmind_node_sdk import ModelUsage
    from agenticmind_providers import ChatResponse, ProviderError
    from agenticmind_providers import streaming as streaming_mod

    monkeypatch.setattr(streaming_mod, "RETRY_BASE_SECONDS", 0)

    class Flaky:
        def __init__(self, fail_times, error, text_before_fail=""):
            self.calls, self.fail_times, self.error, self.text_before_fail = 0, fail_times, error, text_before_fail

        async def stream(self, request, *, api_key=None, base_url=None, on_text=None):
            self.calls += 1
            if self.calls <= self.fail_times:
                if self.text_before_fail:
                    await on_text(self.text_before_fail)
                raise self.error
            await on_text("fine")
            return ChatResponse(text="fine", usage=ModelUsage(provider="x", model="m"))

        async def chat(self, request, *, api_key=None, base_url=None):
            return await self.stream(request, on_text=_noop)

    async def _noop(_):
        return None

    dropped = ProviderError("PROVIDER_UNAVAILABLE", "Chat stream failed: ReadTimeout", retryable=True)
    p = Flaky(2, dropped)
    resp, deltas = await collect(p)
    assert p.calls == 3 and resp.text == "fine" and deltas == ["fine"]  # two retries, then success
    p = Flaky(1, dropped)
    assert (await chat_stream(p, REQ)).text == "fine" and p.calls == 2  # non-streamed calls retry too

    out_of_retries = Flaky(3, dropped)
    not_transient = Flaky(1, ProviderError("PROVIDER_BAD_REQUEST", "bad", retryable=False))
    text_shown = Flaky(1, dropped, text_before_fail="Hel")  # retrying would show the text twice
    for flaky in (out_of_retries, not_transient, text_shown):
        try:
            await collect(flaky)
            raise AssertionError("should have raised")
        except ProviderError:
            pass
    assert [out_of_retries.calls, not_transient.calls, text_shown.calls] == [3, 1, 1]

    from agenticmind_providers.anthropic_chat import AnthropicChat

    assert AnthropicChat.retries_internally is True  # its SDK retries; no second layer

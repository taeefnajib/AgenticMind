"""Reasoning effort (ADR-0041) maps to each provider's documented control, and is omitted where there is none."""

from __future__ import annotations

from agenticmind_providers import ChatMessage, ChatRequest
from agenticmind_providers.anthropic_chat import build_params
from agenticmind_providers.chat import OpenAICompatibleChat
from agenticmind_providers.gemini_chat import build_body as gemini_body

KEY = "fixture-provider-key"


def req(effort=None, model="m"):
    return ChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")], reasoning_effort=effort)


def body(provider, effort):
    return OpenAICompatibleChat(provider)._prepare(req(effort), KEY, "https://example.invalid/v1")[2]


def test_openai_compatible_mapping():
    assert body("openrouter", "low")["reasoning"] == {"effort": "low"}
    assert body("openrouter", "none")["reasoning"] == {"effort": "none"}  # OpenRouter: disables reasoning
    assert body("openai", "minimal")["reasoning_effort"] == "minimal"
    assert body("azure_openai", "high")["reasoning_effort"] == "high"
    for provider in ("groq", "mistral", "deepseek", "together", "xai"):  # no documented control: not sent
        b = body(provider, "low")
        assert "reasoning" not in b and "reasoning_effort" not in b
    default = body("openrouter", None)
    assert "reasoning" not in default and "reasoning_effort" not in default


def test_anthropic_and_gemini_mapping():
    assert build_params(req("high"))["output_config"] == {"effort": "high"}
    assert build_params(req("none"))["output_config"] == {"effort": "low"}  # thinking cannot be switched off
    assert "output_config" not in build_params(req())
    cfg = gemini_body(req("none", "gemini-3.8-flash"))["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingLevel": "MINIMAL"}
    assert gemini_body(req("max"))["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "XHIGH"}
    assert "thinkingConfig" not in gemini_body(req())["generationConfig"]

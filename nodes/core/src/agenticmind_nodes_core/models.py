"""Model nodes (backed by the provider adapter layer)."""

from __future__ import annotations

from typing import Literal

from agenticmind_node_sdk import (
    ExecutionPolicy,
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    TransientNodeError,
    define_node,
    secret_field,
    ui,
)
from agenticmind_providers import ChatMessage, ChatRequest, ProviderError, get_provider
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL, OPENROUTER_BASE_URL
from pydantic import Field, model_validator

from .io import PACKAGE


async def _chat(
    ctx: NodeContext,
    *,
    provider_name: str,
    model: str,
    prompt: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    api_key_ref: SecretRef | None,
    base_url: str | None,
):
    try:
        provider = get_provider(provider_name)
        api_key = ctx.secret(api_key_ref) if api_key_ref else None
        await ctx.emit("model_request", {"provider": provider_name, "model": model})
        return await provider.chat(
            ChatRequest(
                model=model,
                system=system or None,
                messages=[ChatMessage(role="user", content=prompt)],
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            api_key=api_key,
            base_url=base_url,
        )
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc


# -- LLM 1.1.0 ------------------------------------------------------------------------------------
class LLMConfig(NodeConfig):
    provider: Literal["openrouter", "openai_compatible", "mock"] = Field(
        default="openrouter",
        description="openrouter: Gemini and many other models (needs OPENROUTER_API_KEY). "
        "openai_compatible: any OpenAI-style API. mock: offline test responses.",
    )
    model: str = Field(
        default=DEFAULT_OPENROUTER_MODEL,
        min_length=1,
        description="Model ID, e.g. google/gemini-3.8-flash (see openrouter.ai/models).",
    )
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    system_prompt: str = Field(default="", json_schema_extra=ui(widget="textarea"))
    temperature: float = Field(default=0.2, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=1024, ge=16, le=200_000, json_schema_extra=ui(advanced=True))
    base_url: str | None = Field(
        default=None,
        description=f"API base URL (openai_compatible; default for openrouter is {OPENROUTER_BASE_URL}).",
        json_schema_extra=ui(advanced=True),
    )

    @model_validator(mode="after")
    def _check(self) -> LLMConfig:
        if self.provider == "openrouter" and self.api_key is None:
            raise ValueError('openrouter needs an api_key secret reference (e.g. {"$secret": "OPENROUTER_API_KEY"})')
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible needs a base_url")
        return self


@define_node(
    type="agenticmind.models.llm",
    version="1.1.0",
    display_name="LLM",
    category="models",
    package=PACKAGE,
    description="Sends a prompt to a chat model (OpenRouter/Gemini by default) and returns its reply.",
    inputs=[
        InputPort(name="prompt", type=PortType.STRING),
        InputPort(name="system", type=PortType.STRING, required=False, description="Overrides the system prompt"),
    ],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=LLMConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=180, max_attempts=3, idempotent=True),
    tags=["models"],
)
async def llm(ctx: NodeContext, inputs, config: LLMConfig) -> NodeResult:
    response = await _chat(
        ctx,
        provider_name=config.provider,
        model=config.model,
        prompt=inputs["prompt"],
        system=inputs.get("system") or config.system_prompt,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        api_key_ref=config.api_key,
        base_url=config.base_url,
    )
    if not response.text.strip():
        raise NodeError(
            "MODEL_EMPTY_RESPONSE",
            f"The model returned no text (finish reason: {response.finish_reason or 'unknown'})",
        )
    return NodeResult(outputs={"text": response.text}, usage=response.usage)


# -- LLM 1.0.0 (legacy, kept so existing projects keep working) ------------------------------------
class LLMConfigV1(NodeConfig):
    provider: Literal["mock"] = Field(default="mock", description="Legacy version: mock provider only.")
    model: str = Field(default="mock-1", min_length=1)
    system_prompt: str = Field(default="", json_schema_extra=ui(widget="textarea"))
    temperature: float = Field(default=0.0, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=1024, ge=1, le=200_000, json_schema_extra=ui(advanced=True))
    api_key: SecretRef | None = secret_field("Provider credential (not needed for mock)")


@define_node(
    type="agenticmind.models.llm",
    version="1.0.0",
    display_name="LLM (legacy 1.0)",
    category="models",
    package=PACKAGE,
    description="Legacy LLM node (mock provider only). Use version 1.1.0 for real models.",
    inputs=[InputPort(name="prompt", type=PortType.STRING)],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=LLMConfigV1,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
    tags=["models", "legacy"],
)
async def llm_v1(ctx: NodeContext, inputs, config: LLMConfigV1) -> NodeResult:
    response = await _chat(
        ctx,
        provider_name="mock",
        model=config.model,
        prompt=inputs["prompt"],
        system=config.system_prompt,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        api_key_ref=None,
        base_url=None,
    )
    return NodeResult(outputs={"text": response.text}, usage=response.usage)

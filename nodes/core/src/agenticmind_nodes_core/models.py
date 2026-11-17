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
from pydantic import Field

from .io import PACKAGE


class LLMConfig(NodeConfig):
    provider: Literal["mock"] = Field(
        default="mock", description="Model provider. Only the deterministic mock provider ships today."
    )
    model: str = Field(default="mock-1", min_length=1)
    system_prompt: str = Field(default="", json_schema_extra=ui(widget="textarea"))
    temperature: float = Field(default=0.0, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=1024, ge=1, le=200_000, json_schema_extra=ui(advanced=True))
    api_key: SecretRef | None = secret_field("Provider credential (not needed for mock)")


@define_node(
    type="agenticmind.models.llm",
    version="1.0.0",
    display_name="LLM",
    category="models",
    package=PACKAGE,
    description="Sends a prompt to a chat model through the provider adapter layer.",
    inputs=[InputPort(name="prompt", type=PortType.STRING)],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=LLMConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=120, max_attempts=2, idempotent=True),
)
async def llm(ctx: NodeContext, inputs, config: LLMConfig) -> NodeResult:
    try:
        provider = get_provider(config.provider)
        api_key = ctx.secret(config.api_key) if config.api_key else None
        await ctx.emit("model_request", {"provider": config.provider, "model": config.model})
        response = await provider.chat(
            ChatRequest(
                model=config.model,
                system=config.system_prompt or None,
                messages=[ChatMessage(role="user", content=inputs["prompt"])],
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            ),
            api_key=api_key,
        )
    except ProviderError as exc:
        if exc.retryable:
            raise TransientNodeError(exc.code, str(exc)) from exc
        raise NodeError(exc.code, str(exc)) from exc
    return NodeResult(outputs={"text": response.text}, usage=response.usage)

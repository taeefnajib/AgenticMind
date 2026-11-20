"""Model nodes (backed by the provider adapter layer)."""

from __future__ import annotations

import base64
import json
import re
from typing import Literal

from agenticmind_node_sdk import (
    VISION_MEDIA_TYPES,
    ExecutionPolicy,
    InputPort,
    ModelUsage,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    Permissions,
    PortType,
    SecretRef,
    TransientNodeError,
    coerce_images,
    define_node,
    secret_field,
    ui,
)
from agenticmind_providers import (
    CHAT_PROVIDER_HELP,
    ChatMessage,
    ChatProviderName,
    ChatRequest,
    ImagePart,
    ProviderError,
    check_chat_provider,
    get_provider,
)
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL, OPENROUTER_BASE_URL
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    images: list[ImagePart] | None = None,
):
    try:
        provider = get_provider(provider_name)
        api_key = ctx.secret(api_key_ref) if api_key_ref else None
        detail = {"provider": provider_name, "model": model}
        if images:
            detail["images"] = len(images)
        await ctx.emit("model_request", detail)
        return await provider.chat(
            ChatRequest(
                model=model,
                system=system or None,
                messages=[ChatMessage(role="user", content=prompt, images=images or [])],
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
    provider: ChatProviderName = Field(
        default="openrouter",
        description=CHAT_PROVIDER_HELP,
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
        check_chat_provider(self.provider, self.api_key, self.base_url)
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
    return await _run_llm(ctx, inputs, config, images=None)


#: Per image sent to a model; larger photos should be resized before upload.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 16


def load_images(ctx: NodeContext, refs: list[dict]) -> list[ImagePart]:
    """Read image references (from Image Input / Image Loader) into base64 parts for a vision model."""
    if len(refs) > MAX_IMAGES:
        raise NodeError("TOO_MANY_IMAGES", f"At most {MAX_IMAGES} images per request ({len(refs)} given)")
    parts = []
    for ref in refs:
        info = ctx.artifact_info(ref["artifact_id"])
        if info.media_type not in VISION_MEDIA_TYPES:
            raise NodeError(
                "IMAGE_FORMAT_UNSUPPORTED",
                f"{info.filename} is {info.media_type}; vision models take PNG, JPEG, WebP or GIF "
                "(convert it, or read its text with the OCR node)",
            )
        if info.size > MAX_IMAGE_BYTES:
            raise NodeError("IMAGE_TOO_LARGE", f"{info.filename} is larger than {MAX_IMAGE_BYTES // 2**20} MB")
        data = ctx.read_artifact(info.id)
        parts.append(ImagePart(media_type=info.media_type, data_base64=base64.b64encode(data).decode("ascii")))
    return parts


async def _run_llm(ctx: NodeContext, inputs, config: LLMConfig, *, images: list[ImagePart] | None) -> NodeResult:
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
        images=images,
    )
    if not response.text.strip():
        raise NodeError(
            "MODEL_EMPTY_RESPONSE",
            f"The model returned no text (finish reason: {response.finish_reason or 'unknown'})."
            + (
                f" It ran out of output tokens (max_tokens={config.max_tokens}); reasoning models spend part of "
                "that budget thinking, so raise max_tokens."
                if response.finish_reason == "length"
                else ""
            ),
        )
    return NodeResult(outputs={"text": response.text}, usage=response.usage)


# -- LLM 1.2.0: vision (image inputs) --------------------------------------------------------------
@define_node(
    type="agenticmind.models.llm",
    version="1.2.0",
    display_name="LLM",
    category="models",
    package=PACKAGE,
    description="Sends a prompt (and optionally images, for vision models) to a chat model: OpenRouter, "
    "Hugging Face, Ollama or any OpenAI-compatible server. Returns its reply.",
    inputs=[
        InputPort(name="prompt", type=PortType.STRING),
        InputPort(name="system", type=PortType.STRING, required=False, description="Overrides the system prompt"),
        InputPort(name="image", type=PortType.IMAGE, required=False, description="An image for a vision model"),
        InputPort(
            name="images", type=PortType.JSON, required=False, description="Several images (Image Loader 'images')"
        ),
    ],
    outputs=[OutputPort(name="text", type=PortType.STRING)],
    config=LLMConfig,
    permissions=Permissions(network=True, credentials=True, artifacts=True),
    execution=ExecutionPolicy(timeout_seconds=240, max_attempts=3, idempotent=True),
    tags=["models", "vision"],
)
async def llm_v12(ctx: NodeContext, inputs, config: LLMConfig) -> NodeResult:
    try:
        refs = coerce_images(inputs.get("image")) + coerce_images(inputs.get("images"))
    except ValueError as exc:
        raise NodeError("INPUT_TYPE_MISMATCH", str(exc)) from exc
    # the same file connected twice is sent once
    unique = list({r["artifact_id"]: r for r in refs}.values())
    return await _run_llm(ctx, inputs, config, images=load_images(ctx, unique) if unique else None)


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


# -- Model Router 1.0.0 ---------------------------------------------------------------------------
ROUTE_NAME = r"^[a-z0-9][a-z0-9_-]{0,39}$"
#: A small, fast model is enough to pick a route.
DEFAULT_CLASSIFIER_MODEL = "google/gemini-3.1-flash-lite"
CLASSIFIER_PROMPT_CHARS = 4000


class ModelRoute(BaseModel):
    """One kind of task and the model that handles it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=ROUTE_NAME, description="Short id, e.g. code or reasoning")
    description: str = Field(default="", max_length=500, description="What kind of request belongs here")
    keywords: list[str] = Field(default_factory=list, max_length=50, description="Words that select this route")
    model: str = Field(min_length=1, max_length=200, description="Model id for this route")
    system_prompt: str = Field(default="", max_length=20_000, description="Replaces the node's system prompt")


def _default_routes() -> list[ModelRoute]:
    return [
        ModelRoute(
            name="general",
            description="Everyday questions, explanations and writing that need no special skill.",
            model=DEFAULT_OPENROUTER_MODEL,
        ),
        ModelRoute(
            name="quick",
            description="Very short tasks: greetings, one-line facts, yes/no answers, classification, rewording.",
            keywords=["translate", "rephrase", "summarize in one sentence", "yes or no"],
            model="google/gemini-3.1-flash-lite",
        ),
        ModelRoute(
            name="reasoning",
            description="Maths, logic puzzles, multi-step analysis, planning and careful comparisons.",
            keywords=["prove", "calculate", "step by step", "analyze", "analyse", "compare", "plan"],
            model="anthropic/claude-sonnet-5.5",
        ),
        ModelRoute(
            name="code",
            description="Writing, explaining, reviewing or debugging code, SQL, regexes and shell commands.",
            keywords=["code", "function", "bug", "python", "javascript", "typescript", "sql", "regex", "stack trace"],
            model="qwen/qwen3-coder",
        ),
    ]


class ModelRouterConfig(NodeConfig):
    provider: ChatProviderName = Field(
        default="openrouter",
        description="Used for every route and the classifier. OpenRouter reaches most models with one key.",
    )
    api_key: SecretRef | None = secret_field("API key secret, e.g. OPENROUTER_API_KEY")
    strategy: Literal["hybrid", "classifier", "keywords"] = Field(
        default="hybrid",
        description="keywords: first route (top to bottom) whose keyword appears; free and predictable. "
        "classifier: a small model reads the request and the route descriptions and picks one. "
        "hybrid: keywords first, the classifier only when no keyword matches.",
    )
    routes: list[ModelRoute] = Field(
        default=_default_routes(),  # a plain default (not a factory) so the editor shows the routes
        min_length=1,
        max_length=12,
        json_schema_extra=ui(widget="model_routes"),
    )
    default_route: str = Field(
        default="general",
        description="Used when nothing matches or the classifier gives no valid answer.",
        json_schema_extra=ui(widget="route_ref"),
    )
    classifier_model: str = Field(default=DEFAULT_CLASSIFIER_MODEL, min_length=1, max_length=200)
    system_prompt: str = Field(
        default="", description="Default system prompt (a route's own prompt replaces it).",
        json_schema_extra=ui(widget="textarea"),
    )  # fmt: skip
    temperature: float = Field(default=0.2, ge=0, le=2, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(default=2048, ge=16, le=200_000, json_schema_extra=ui(advanced=True))
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))

    @model_validator(mode="after")
    def _check(self) -> ModelRouterConfig:
        names = [r.name for r in self.routes]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"route names must be unique (repeated: {', '.join(dupes)})")
        if self.default_route not in names:
            raise ValueError(f"default_route {self.default_route!r} is not one of the routes ({', '.join(names)})")
        check_chat_provider(self.provider, self.api_key, self.base_url)
        return self


def keyword_route(text: str, routes: list[ModelRoute]) -> tuple[ModelRoute, str] | None:
    """The first route (in order) with a keyword in ``text``: whole words/phrases, case-insensitive."""
    lowered = text.lower()
    for route in routes:
        for keyword in route.keywords:
            k = keyword.strip().lower()
            if k and re.search(rf"(?<!\w){re.escape(k)}(?!\w)", lowered):
                return route, f"keyword {keyword.strip()!r}"
    return None


def classifier_request(prompt: str, routes: list[ModelRoute], model: str) -> ChatRequest:
    listing = "\n".join(f"- {r.name}: {r.description or '(no description)'}" for r in routes)
    excerpt = prompt[:CLASSIFIER_PROMPT_CHARS]
    return ChatRequest(
        model=model,
        system=(
            "You route requests to the best-suited model. Read the request and choose exactly one route. "
            'Reply with JSON only: {"route": "<route name>", "reason": "<at most 12 words>"}.'
        ),
        messages=[ChatMessage(role="user", content=f"Routes:\n{listing}\n\nRequest:\n<<<\n{excerpt}\n>>>")],
        temperature=0,
        max_tokens=512,  # reasoning models think before answering
    )


def parse_route_choice(text: str, names: set[str]) -> tuple[str, str] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict) and str(value.get("route", "")).strip() in names:
            return str(value["route"]).strip(), str(value.get("reason", ""))[:200]
    bare = text.strip().strip("\"'`.").lower()
    return (bare, "") if bare in names else None


def _add_usage(a: ModelUsage | None, b: ModelUsage | None) -> ModelUsage | None:
    if a is None or b is None:
        return a or b
    cost = None
    if a.estimated_cost_usd is not None or b.estimated_cost_usd is not None:
        cost = (a.estimated_cost_usd or 0) + (b.estimated_cost_usd or 0)
    return ModelUsage(
        provider=b.provider,
        model=b.model,
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        estimated_cost_usd=cost,
    )


@define_node(
    type="agenticmind.models.model_router",
    version="1.0.0",
    display_name="Model Router",
    category="models",
    package=PACKAGE,
    description="Picks the best model for each request (by keywords, a small classifier model, or both) and "
    "answers with it: e.g. code to a coding model, maths to a reasoning model, quick tasks to a cheap one.",
    inputs=[
        InputPort(name="prompt", type=PortType.STRING),
        InputPort(name="system", type=PortType.STRING, required=False, description="Overrides the system prompt"),
        InputPort(
            name="route", type=PortType.STRING, required=False, description="Force a route by name (skips routing)"
        ),
    ],
    outputs=[
        OutputPort(name="text", type=PortType.STRING, description="The chosen model's reply"),
        OutputPort(name="route", type=PortType.STRING, description="Name of the route that was used"),
        OutputPort(name="model", type=PortType.STRING, description="Model id that answered"),
    ],
    config=ModelRouterConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=240, max_attempts=3, idempotent=True),
    tags=["models", "routing"],
)
async def model_router(ctx: NodeContext, inputs, config: ModelRouterConfig) -> NodeResult:
    prompt: str = inputs["prompt"]
    by_name = {r.name: r for r in config.routes}
    route: ModelRoute | None = None
    how, reason = "default", "nothing matched"
    usage: ModelUsage | None = None

    forced = (inputs.get("route") or "").strip()
    if forced:
        if forced not in by_name:
            raise NodeError("ROUTE_UNKNOWN", f"No route named {forced!r} (routes: {', '.join(by_name)})")
        route, how, reason = by_name[forced], "input", "route given on the input"
    if route is None and config.strategy in ("keywords", "hybrid"):
        hit = keyword_route(prompt, config.routes)
        if hit:
            route, reason = hit
            how = "keywords"
    if route is None and config.strategy in ("classifier", "hybrid") and len(config.routes) > 1:
        try:
            provider = get_provider(config.provider)
            api_key = ctx.secret(config.api_key) if config.api_key else None
            answer = await provider.chat(
                classifier_request(prompt, config.routes, config.classifier_model),
                api_key=api_key,
                base_url=config.base_url,
            )
            usage = answer.usage
            choice = parse_route_choice(answer.text, set(by_name))
            if choice:
                route, how = by_name[choice[0]], "classifier"
                reason = choice[1] or "chosen by the classifier"
            else:
                reason = "the classifier gave no valid route"
        except ProviderError as exc:
            if exc.retryable:
                raise TransientNodeError(exc.code, str(exc)) from exc
            reason = f"classifier failed ({exc.code})"
    if route is None:
        route = by_name[config.default_route]

    await ctx.emit("model_route", {"route": route.name, "model": route.model, "how": how, "reason": reason})
    response = await _chat(
        ctx,
        provider_name=config.provider,
        model=route.model,
        prompt=prompt,
        system=inputs.get("system") or route.system_prompt or config.system_prompt,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        api_key_ref=config.api_key,
        base_url=config.base_url,
    )
    if not response.text.strip():
        raise NodeError(
            "MODEL_EMPTY_RESPONSE",
            f"{route.model} (route {route.name}) returned no text (finish reason: "
            f"{response.finish_reason or 'unknown'}). If it ran out of tokens, raise max_tokens.",
        )
    return NodeResult(
        outputs={"text": response.text, "route": route.name, "model": route.model},
        usage=_add_usage(usage, response.usage),
    )

"""Classifier and Summarizer: everyday LLM text tasks with predictable, typed outputs.

* **Classifier** labels a text with one (or several) of the configured labels. Each label is also a
  control branch, so a graph can route by label; unmatched or low-confidence input goes to ``default``.
* **Summarizer** summarises text, a Document or a DocumentSet with ``stuff`` (one call), ``map_reduce``
  (summarise chunks, then combine; hierarchical when the partial summaries are still long) or ``refine``
  (walk the chunks, updating a running summary). ``auto`` picks ``stuff`` when the text fits in one chunk.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from agenticmind_node_sdk import (
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
    define_node,
    secret_field,
    ui,
)
from agenticmind_providers import (
    ChatMessage,
    ChatProviderName,
    ChatRequest,
    ChatResponse,
    ProviderError,
    ToolSpec,
    check_chat_provider,
    get_provider,
)
from agenticmind_providers.chat import DEFAULT_OPENROUTER_MODEL
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .io import PACKAGE


class _ModelConfig(NodeConfig):
    provider: ChatProviderName = "openrouter"
    model: str = DEFAULT_OPENROUTER_MODEL
    api_key: SecretRef | None = secret_field("API key, e.g. OPENROUTER_API_KEY")
    base_url: str | None = Field(default=None, json_schema_extra=ui(advanced=True))
    max_tokens: int = Field(
        default=2000, ge=64, le=32000,
        description="Output token cap per call; reasoning models spend part of it thinking.",
        json_schema_extra=ui(advanced=True),
    )  # fmt: skip

    @model_validator(mode="after")
    def _provider(self) -> _ModelConfig:
        check_chat_provider(self.provider, self.api_key, self.base_url)
        return self


class _Meter:
    def __init__(self, config: _ModelConfig) -> None:
        self.config = config
        self.calls = 0
        self.tokens_in = self.tokens_out = 0
        self.cost: float | None = 0.0

    async def chat(self, ctx: NodeContext, request: ChatRequest) -> ChatResponse:
        c = self.config
        try:
            api_key = ctx.secret(c.api_key) if c.api_key else None
            response = await get_provider(c.provider).chat(request, api_key=api_key, base_url=c.base_url)
        except ProviderError as exc:
            if exc.retryable:
                raise TransientNodeError(exc.code, str(exc)) from exc
            raise NodeError(exc.code, str(exc)) from exc
        self.calls += 1
        self.tokens_in += response.usage.input_tokens
        self.tokens_out += response.usage.output_tokens
        if response.usage.estimated_cost_usd is None:
            self.cost = None
        elif self.cost is not None:
            self.cost += response.usage.estimated_cost_usd
        return response

    def usage(self) -> ModelUsage:
        return ModelUsage(
            provider=self.config.provider,
            model=self.config.model,
            input_tokens=self.tokens_in,
            output_tokens=self.tokens_out,
            estimated_cost_usd=round(self.cost, 6) if self.cost is not None else None,
        )


def as_text(value: Any) -> str:
    """Text of a string, Document ({text}), DocumentSet (list of documents) or other JSON value."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    if isinstance(value, dict) and isinstance(value.get("documents"), list):
        return as_text(value["documents"])
    if isinstance(value, list):
        return "\n\n".join(as_text(v) for v in value)
    return "" if value is None else json.dumps(value, ensure_ascii=False)


# ---- Classifier -----------------------------------------------------------------------------------
class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = ""
    examples: list[str] = Field(default_factory=list, description="Optional example inputs (few-shot).")


DEFAULT_LABELS = [
    {"name": "positive", "description": "Happy, satisfied, praising"},
    {"name": "negative", "description": "Unhappy, complaining, angry"},
    {"name": "neutral", "description": "Factual or mixed, no clear feeling"},
]


class ClassifierConfig(_ModelConfig):
    labels: list[Label] = Field(
        default=[Label.model_validate(x) for x in DEFAULT_LABELS],
        description="Each label is also an output branch. Unmatched or low-confidence input goes to 'default'.",
    )
    multi_label: bool = Field(default=False, description="Allow several labels (all matching branches run).")
    min_confidence: float = Field(
        default=0.0, ge=0, le=1, description="Below this confidence the input goes to 'default'."
    )
    instructions: str = Field(default="", description="Extra guidance for the classifier.",
                              json_schema_extra=ui(widget="textarea", advanced=True))  # fmt: skip

    @model_validator(mode="after")
    def _labels(self) -> ClassifierConfig:
        names = [x.name for x in self.labels]
        if len(names) < 2:
            raise ValueError("define at least two labels")
        if len(names) != len(set(names)):
            raise ValueError("label names must be unique")
        if "default" in names:
            raise ValueError("'default' is reserved for unmatched input")
        return self


@define_node(
    type="agenticmind.core.classifier",
    version="1.0.0",
    display_name="Classifier",
    category="models",
    package=PACKAGE,
    description="Labels text with one (or several) of your labels, with a confidence; each label is a branch.",
    inputs=[InputPort(name="text", type=PortType.ANY, description="Text, Document or JSON to classify")],
    outputs=[
        OutputPort(name="label", type=PortType.STRING, description="Best label, or 'default'"),
        OutputPort(name="labels", type=PortType.JSON, description="All chosen labels (multi-label)"),
        OutputPort(name="confidence", type=PortType.NUMBER),
        OutputPort(name="reason", type=PortType.STRING),
    ],
    branches=["default"],
    branches_from_config="labels",
    config=ClassifierConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=60, max_attempts=2, idempotent=True),
    tags=["model", "classification", "routing"],
)
async def classifier(ctx: NodeContext, inputs, config: ClassifierConfig) -> NodeResult:
    text = as_text(inputs["text"]).strip()
    if not text:
        raise NodeError("TEXT_EMPTY", "Nothing to classify")
    names = [x.name for x in config.labels]
    listing = []
    for x in config.labels:
        line = f"- {x.name}: {x.description or x.name}"
        if x.examples:
            line += "\n  examples: " + " | ".join(x.examples[:5])
        listing.append(line)
    label_schema: dict[str, Any] = {"type": "string", "enum": [*names, "none"]}
    properties: dict[str, Any] = (
        {"labels": {"type": "array", "items": label_schema}} if config.multi_label else {"label": label_schema}
    )
    properties["confidence"] = {"type": "number", "minimum": 0, "maximum": 1}
    properties["reason"] = {"type": "string", "description": "One short sentence"}
    task = "every label that applies" if config.multi_label else "the single best label"
    request = ChatRequest(
        model=config.model,
        system=f"You classify text. Choose {task} by calling classify; use 'none' when no label fits.\n"
        + (f"{config.instructions.strip()}\n" if config.instructions.strip() else "")
        + "\nLabels:\n"
        + "\n".join(listing),
        messages=[ChatMessage(role="user", content=text[:20000])],
        temperature=0,
        max_tokens=config.max_tokens,
        tools=[
            ToolSpec(
                name="classify",
                description="Report the classification",
                parameters={
                    "type": "object",
                    "properties": properties,
                    "required": [next(iter(properties)), "confidence"],
                },
            )
        ],  # fmt: skip
        tool_choice="required",
    )
    meter = _Meter(config)
    response = await meter.chat(ctx, request)
    args: dict[str, Any] = next((c.arguments for c in response.tool_calls if c.name == "classify"), {})
    if not args:  # text fallback: the first label named in the reply
        found = [n for n in names if re.search(rf"\b{re.escape(n)}\b", response.text, re.I)]
        args = {"labels": found[:1] if not config.multi_label else found, "confidence": 0.5}
    raw = args.get("labels", [args.get("label")]) if config.multi_label or "labels" in args else [args.get("label")]
    chosen = [n for n in names if n in {str(x).strip().lower() for x in raw or [] if x}]
    try:
        confidence = max(0.0, min(1.0, float(args.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    reason = str(args.get("reason", ""))
    if not chosen or confidence < config.min_confidence:
        if chosen:
            reason = f"confidence {confidence:.2f} below {config.min_confidence:.2f}: {reason}".strip()
        return NodeResult(
            outputs={"label": "default", "labels": [], "confidence": confidence, "reason": reason or "no label fits"},
            branches=frozenset({"default"}),
            usage=meter.usage(),
        )
    if not config.multi_label:
        chosen = chosen[:1]
    return NodeResult(
        outputs={"label": chosen[0], "labels": chosen, "confidence": confidence, "reason": reason},
        branches=frozenset(chosen),
        usage=meter.usage(),
    )


# ---- Summarizer -----------------------------------------------------------------------------------
Strategy = Literal["auto", "stuff", "map_reduce", "refine"]
STYLES = {
    "paragraph": "a concise paragraph",
    "bullets": "a bulleted list of the key points",
    "tldr": "one or two sentences (TL;DR)",
    "detailed": "a structured summary with short headed sections",
}


class SummarizerConfig(_ModelConfig):
    strategy: Strategy = Field(
        default="auto",
        description="stuff: one call. map_reduce: summarise chunks, then combine. refine: update a running "
        "summary chunk by chunk. auto: stuff when it fits in one chunk, else map_reduce.",
    )
    style: Literal["paragraph", "bullets", "tldr", "detailed"] = "paragraph"
    max_words: int = Field(default=200, ge=10, le=4000, description="Target length of the final summary.")
    focus: str = Field(default="", description="Optional focus, e.g. 'risks and deadlines'.")
    chunk_chars: int = Field(default=12000, ge=500, le=200000, json_schema_extra=ui(advanced=True))
    max_chunks: int = Field(default=50, ge=1, le=500, description="Refuse longer inputs instead of paying for them.",
                            json_schema_extra=ui(advanced=True))  # fmt: skip
    temperature: float = Field(default=0.2, ge=0, le=2, json_schema_extra=ui(advanced=True))


def split_text(text: str, size: int) -> list[str]:
    """Chunks of at most ``size`` characters, cut at paragraph, line or sentence ends when possible."""
    chunks: list[str] = []
    rest = text.strip()
    while len(rest) > size:
        window = rest[:size]
        cut = size
        for sep in ("\n\n", "\n", ". ", " "):  # prefer the strongest boundary in the second half
            at = window.rfind(sep)
            if at > size // 2:
                cut = at + len(sep)
                break
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks


@define_node(
    type="agenticmind.core.summarizer",
    version="1.0.0",
    display_name="Summarizer",
    category="models",
    package=PACKAGE,
    description="Summarises long text or documents with stuff, map-reduce or refine.",
    inputs=[InputPort(name="text", type=PortType.ANY, description="Text, Document or DocumentSet")],
    outputs=[
        OutputPort(name="summary", type=PortType.STRING),
        OutputPort(name="result", type=PortType.JSON, description="Strategy, chunk count, partial summaries"),
    ],
    config=SummarizerConfig,
    permissions=Permissions(network=True, credentials=True),
    execution=ExecutionPolicy(timeout_seconds=600, max_attempts=2, idempotent=True),
    tags=["model", "summarization"],
)
async def summarizer(ctx: NodeContext, inputs, config: SummarizerConfig) -> NodeResult:
    text = as_text(inputs["text"]).strip()
    if not text:
        raise NodeError("TEXT_EMPTY", "Nothing to summarise")
    chunks = split_text(text, config.chunk_chars)
    if len(chunks) > config.max_chunks:
        raise NodeError(
            "TEXT_TOO_LONG", f"The input splits into {len(chunks)} chunks (max_chunks is {config.max_chunks})"
        )
    strategy = config.strategy
    if strategy == "auto":
        strategy = "stuff" if len(chunks) == 1 else "map_reduce"
    if strategy == "stuff" and len(chunks) > 1:
        chunks = [text]  # the user asked for one call; the provider enforces the context limit
    meter = _Meter(config)
    focus = f" Focus on: {config.focus.strip()}." if config.focus.strip() else ""
    shape = f"Write {STYLES[config.style]}, at most about {config.max_words} words.{focus}"

    async def ask(prompt: str, words: int) -> str:
        request = ChatRequest(
            model=config.model,
            system="You write faithful summaries. Use only facts from the text; do not add outside knowledge.",
            messages=[ChatMessage(role="user", content=prompt)],
            temperature=config.temperature,
            max_tokens=max(config.max_tokens, int(words * 2.5) + 200),
        )
        return (await meter.chat(ctx, request)).text.strip()

    partials: list[str] = []
    if strategy == "stuff":
        summary = await ask(f"{shape}\n\nText:\n{chunks[0]}", config.max_words)
    elif strategy == "refine":
        summary = await ask(f"{shape}\n\nText (part 1 of {len(chunks)}):\n{chunks[0]}", config.max_words)
        partials.append(summary)
        for i, chunk in enumerate(chunks[1:], start=2):
            summary = await ask(
                f"Current summary:\n{summary}\n\nMore text (part {i} of {len(chunks)}):\n{chunk}\n\n"
                f"Update the summary with any important new information. {shape}",
                config.max_words,
            )
            partials.append(summary)
            await ctx.emit("summary_progress", {"part": i, "of": len(chunks)})
    else:  # map_reduce, hierarchical while the combined partials are still too long
        per_chunk = max(60, min(400, config.max_words))
        level = chunks
        while True:
            mapped = []
            for i, chunk in enumerate(level, start=1):
                mapped.append(await ask(f"Summarise this part of a longer text in at most about {per_chunk} "
                                        f"words, keeping key facts, names and numbers.{focus}\n\nText:\n{chunk}",
                                        per_chunk))  # fmt: skip
                await ctx.emit("summary_progress", {"part": i, "of": len(level)})
            partials.extend(mapped)
            joined = "\n\n".join(f"[Part {i}]\n{m}" for i, m in enumerate(mapped, start=1))
            next_level = split_text(joined, config.chunk_chars)
            # stop when it fits, or when another level would not shrink it (tiny chunk sizes)
            if len(joined) <= config.chunk_chars or len(mapped) == 1 or len(next_level) >= len(level):
                break
            level = next_level
        summary = await ask(f"These are summaries of consecutive parts of one text. Combine them into one "
                            f"summary. {shape}\n\n{joined}", config.max_words)  # fmt: skip
    return NodeResult(
        outputs={
            "summary": summary,
            "result": {"strategy": strategy, "chunks": len(chunks), "calls": meter.calls, "partials": partials},
        },
        usage=meter.usage(),
    )

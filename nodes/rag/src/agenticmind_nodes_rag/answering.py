"""Context Builder and Citation Builder: turn retrieved chunks into a grounded, cited answer.

Flow: Retriever → Context Builder (numbered sources + prompt, within a token budget) → LLM →
Citation Builder (maps [n] markers in the answer back to the sources and validates them).
"""

from __future__ import annotations

import re
from typing import Any, Literal

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
    ui,
)
from agenticmind_node_sdk.documents import parse_retrieved
from pydantic import Field, model_validator

from .chunking import Measurer

PACKAGE = "agenticmind-nodes-rag"

DEFAULT_INSTRUCTIONS = (
    "Answer the question using only the numbered sources below.\n"
    "Cite every claim inline with the source number in square brackets, e.g. [1] or [2][3].\n"
    "If the sources do not contain the answer, say that you don't know. Do not invent sources.\n\n"
    "Sources:\n{{context}}\n\n"
    "Question: {{question}}"
)
EXCERPT_CHARS = 280


def _tokens(text: str) -> int:
    return Measurer(text, "tokens")(0, len(text))


def _truncate_tokens(text: str, limit: int) -> str:
    m = Measurer(text, "tokens")
    if limit <= 0 or not m.ends:
        return ""
    end = m.ends[min(limit, len(m.ends)) - 1]
    return text[:end].rstrip() + " …"


def _header(n: int, doc: Any) -> str:
    parts = [doc.source.filename or doc.source.uri]
    if doc.source.page:
        parts.append(f"p. {doc.source.page}")
    if doc.source.section:
        parts.append(doc.source.section)
    return f"[{n}] " + " · ".join(parts)


# -- Context Builder -------------------------------------------------------------------------------
class ContextBuilderConfig(NodeConfig):
    token_budget: int = Field(
        default=3000, ge=50, le=200_000, description="Maximum approximate tokens of source text in the prompt."
    )
    order: Literal["relevance", "document"] = Field(
        default="relevance",
        description="relevance: best match first. document: in reading order (file, page, position).",
    )
    deduplicate: bool = Field(default=True, description="Drop repeated or overlapping chunks.")
    instructions: str = Field(
        default=DEFAULT_INSTRUCTIONS,
        description="Prompt template. Must contain {{context}} and {{question}}.",
        json_schema_extra=ui(widget="textarea", advanced=True),
    )

    @model_validator(mode="after")
    def _placeholders(self) -> ContextBuilderConfig:
        missing = [p for p in ("{{context}}", "{{question}}") if p not in self.instructions]
        if missing:
            raise ValueError(f"instructions must contain {' and '.join(missing)}")
        return self


def _overlaps(a: Any, b: Any) -> bool:
    if a.parent_id is None or a.parent_id != b.parent_id or a.offsets is None or b.offsets is None:
        return False
    shorter = min(a.offsets.end - a.offsets.start, b.offsets.end - b.offsets.start) or 1
    overlap = min(a.offsets.end, b.offsets.end) - max(a.offsets.start, b.offsets.start)
    return overlap / shorter >= 0.8


@define_node(
    type="agenticmind.rag.context_builder",
    version="1.0.0",
    display_name="Context Builder",
    category="rag",
    package=PACKAGE,
    description="Numbers the retrieved chunks as sources, fits them into a token budget and builds the prompt.",
    inputs=[
        InputPort(name="documents", type=PortType.DOCUMENT_SET),
        InputPort(name="question", type=PortType.STRING),
    ],
    outputs=[
        OutputPort(name="prompt", type=PortType.STRING, description="Ready-to-send prompt for the LLM"),
        OutputPort(name="context", type=PortType.STRING, description="Numbered source text only"),
        OutputPort(name="sources", type=PortType.JSON, description="Source list for the Citation Builder"),
    ],
    config=ContextBuilderConfig,
    tags=["rag"],
)
async def context_builder(ctx: NodeContext, inputs, config: ContextBuilderConfig) -> NodeResult:
    docs = parse_retrieved_or_plain(inputs["documents"])
    if config.order == "document":
        docs = sorted(
            docs,
            key=lambda d: (d.source.filename or d.source.uri, d.source.page or 0, d.offsets.start if d.offsets else 0),
        )

    kept: list[Any] = []
    duplicates = 0
    for d in docs:
        if config.deduplicate and any(d.content.strip() == k.content.strip() or _overlaps(d, k) for k in kept):
            duplicates += 1
            continue
        kept.append(d)

    blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    used = 0
    over_budget = 0
    for d in kept:
        n = len(sources) + 1
        block = f"{_header(n, d)}\n{d.content.strip()}"
        cost = _tokens(block)
        if used + cost > config.token_budget:
            if not blocks:  # always include at least one source, truncated to the budget
                block = _truncate_tokens(block, config.token_budget)
                cost = _tokens(block)
            else:
                over_budget += 1
                continue
        blocks.append(block)
        used += cost
        sources.append(
            {
                "n": n,
                "id": d.id,
                "uri": d.source.uri,
                "filename": d.source.filename,
                "page": d.source.page,
                "section": d.source.section,
                "score": getattr(d, "score", None),
                "offsets": d.offsets.model_dump() if d.offsets else None,
                "excerpt": d.content.strip()[:EXCERPT_CHARS],
            }
        )

    context = "\n\n".join(blocks) if blocks else "(no sources were retrieved)"
    prompt = config.instructions.replace("{{context}}", context).replace("{{question}}", inputs["question"].strip())
    await ctx.emit(
        "context_built",
        {"sources": len(sources), "tokens": used, "dropped_duplicates": duplicates, "dropped_over_budget": over_budget},
    )
    return NodeResult(outputs={"prompt": prompt, "context": context, "sources": sources})


def parse_retrieved_or_plain(value: Any) -> list[Any]:
    """Accept retrieved documents (with scores) and plain documents alike."""
    from agenticmind_node_sdk.documents import parse_documents

    try:
        return parse_retrieved(value)
    except Exception:  # noqa: BLE001 - fall back to plain documents (no retrieval fields)
        return parse_documents(value)


# -- Citation Builder ------------------------------------------------------------------------------
_MARKER = re.compile(r"\[(\d+(?:\s*(?:[-–,]\s*\d+))*)\]")


def _expand(group: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", group):
        if re.fullmatch(r"\d+\s*[-–]\s*\d+", part):
            a, b = (int(x) for x in re.split(r"\s*[-–]\s*", part))
            if 0 < b - a <= 50:
                out.extend(range(a, b + 1))
            else:
                out.extend([a, b])
        elif part.strip().isdigit():
            out.append(int(part))
    return out


class CitationBuilderConfig(NodeConfig):
    require_citations: bool = Field(default=False, description="Fail the node when the answer cites no valid source.")
    remove_invalid: bool = Field(
        default=True, description="Remove markers that point to sources that do not exist (e.g. [9] with 4 sources)."
    )


@define_node(
    type="agenticmind.rag.citation_builder",
    version="1.0.0",
    display_name="Citation Builder",
    category="rag",
    package=PACKAGE,
    description="Links [n] markers in the answer to their sources and checks that every citation is valid.",
    inputs=[
        InputPort(name="answer", type=PortType.STRING),
        InputPort(name="sources", type=PortType.JSON),
    ],
    outputs=[
        OutputPort(name="answer", type=PortType.STRING),
        OutputPort(name="citations", type=PortType.JSON, description="Cited sources, in order of first use"),
        OutputPort(name="result", type=PortType.JSON, description="{answer, citations, validation} for chat/API"),
    ],
    config=CitationBuilderConfig,
    tags=["rag"],
)
async def citation_builder(ctx: NodeContext, inputs, config: CitationBuilderConfig) -> NodeResult:
    sources = inputs["sources"]
    if not isinstance(sources, list) or not all(isinstance(s, dict) and isinstance(s.get("n"), int) for s in sources):
        raise NodeError("INPUT_TYPE_MISMATCH", "'sources' must be the source list from a Context Builder")
    by_n = {s["n"]: s for s in sources}
    answer = inputs["answer"]

    cited: list[int] = []
    invalid: list[int] = []
    for match in _MARKER.finditer(answer):
        for n in _expand(match.group(1)):
            if n in by_n:
                if n not in cited:
                    cited.append(n)
            elif n not in invalid:
                invalid.append(n)

    if config.remove_invalid and invalid:

        def clean(match: re.Match[str]) -> str:
            valid = [n for n in _expand(match.group(1)) if n in by_n]
            return "".join(f"[{n}]" for n in valid)

        answer = re.sub(r"[ \t]+([.,;:!?])", r"\1", _MARKER.sub(clean, answer))

    citations = [by_n[n] for n in cited]
    validation = {
        "has_citations": bool(cited),
        "cited": cited,
        "invalid": invalid,
        "uncited": [s["n"] for s in sources if s["n"] not in cited],
    }
    if config.require_citations and not cited:
        raise NodeError("CITATIONS_MISSING", "The answer does not cite any of the provided sources")
    await ctx.emit("citations_checked", validation)
    return NodeResult(
        outputs={
            "answer": answer,
            "citations": citations,
            "result": {"answer": answer, "citations": citations, "validation": validation},
        }
    )

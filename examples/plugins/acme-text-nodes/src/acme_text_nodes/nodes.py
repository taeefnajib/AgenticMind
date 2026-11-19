"""Example node: a typed contract and an async handler."""

from __future__ import annotations

import re
from collections import Counter

from agenticmind_node_sdk import (
    InputPort,
    NodeConfig,
    NodeContext,
    NodeError,
    NodeResult,
    OutputPort,
    PortType,
    define_node,
)
from pydantic import Field

PACKAGE = "acme-text-nodes"


class WordStatsConfig(NodeConfig):
    top_n: int = Field(default=5, ge=1, le=50, description="How many frequent words to report.")
    min_length: int = Field(default=3, ge=1, le=20, description="Ignore words shorter than this.")


@define_node(
    type="acme.text.word_stats",
    version="1.0.0",
    display_name="Word Statistics",
    category="transform",
    package=PACKAGE,
    description="Counts words and sentences and lists the most frequent words in a text.",
    inputs=[InputPort(name="text", type=PortType.STRING)],
    outputs=[
        OutputPort(name="stats", type=PortType.JSON, description="{words, sentences, top_words}"),
        OutputPort(name="summary", type=PortType.STRING),
    ],
    config=WordStatsConfig,
    # no permissions needed: it only computes. Declare network/credentials/... only if you use them.
    tags=["text", "acme"],
)
async def word_stats(ctx: NodeContext, inputs, config: WordStatsConfig) -> NodeResult:
    text = str(inputs.get("text") or "")
    if not text.strip():
        raise NodeError("TEXT_EMPTY", "The text is empty")
    words = [w.lower() for w in re.findall(r"[\w']+", text) if len(w) >= config.min_length]
    sentences = max(1, len(re.findall(r"[.!?]+(?:\s|$)", text)))
    top = Counter(words).most_common(config.top_n)
    await ctx.emit("word_stats", {"words": len(words)})
    stats = {"words": len(words), "sentences": sentences, "top_words": [{"word": w, "count": c} for w, c in top]}
    summary = f"{len(words)} words in {sentences} sentence(s); most frequent: " + ", ".join(w for w, _ in top)
    return NodeResult(outputs={"stats": stats, "summary": summary})

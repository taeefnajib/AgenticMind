"""Chunking strategies of Chunker 1.1.0 and the Parent Expander (ADR-0027)."""

from __future__ import annotations

import pytest
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_rag.chunker import ChunkerConfigV11, chunker, chunker_v11
from agenticmind_nodes_rag.chunking import (
    DEFAULT_SEPARATORS,
    Measurer,
    layout_blocks,
    layout_chunks,
    lexical_vectors,
    pack_spans,
    semantic_spans,
    sentence_spans,
    sentence_windows,
)
from agenticmind_nodes_rag.parents import parent_expander

HANDBOOK = """# Leave policy

Everyone gets 25 days of annual leave. Leave must be approved by your manager. Unused leave carries over.

| Type | Days |
|---|---|
| Annual | 25 |
| Sick | 10 |

## Travel

- Book trains for trips under four hours.
- Flights need director approval
  and a business reason.

```
expense --submit receipt.pdf
```
"""

TOPICS = (
    "Cats are small carnivorous mammals. Cats purr and groom their fur. Many cats sleep most of the day. "
    "Domestic cats hunt mice and birds. "
    "Volcanoes erupt molten lava and ash. Magma rises through the volcano crust. Volcanic eruptions reshape "
    "mountains. Lava cools into volcanic basalt rock."
)


def doc(content: str, id: str = "d1") -> dict:
    return {"id": id, "content": content, "source": {"uri": f"artifact://{id}", "filename": f"{id}.md"}}


def test_sentences_and_packing_keep_whole_sentences():
    text = "One two three. Four five six? Seven eight!\n\nNine ten."
    spans = sentence_spans(text, 0, len(text))
    assert [text[a:b] for a, b in spans] == ["One two three.", "Four five six?", "Seven eight!", "Nine ten."]
    m = Measurer(text, "tokens")
    packed = pack_spans(text, spans, 8, 4, m, DEFAULT_SEPARATORS)
    assert [text[a:b] for a, b in packed] == [
        "One two three. Four five six?",
        "Four five six? Seven eight!",  # overlap repeats whole trailing sentences
        "Seven eight!\n\nNine ten.",
    ]


def test_semantic_breaks_where_the_topic_changes():
    sentences = sentence_spans(TOPICS, 0, len(TOPICS))
    vectors = lexical_vectors(sentence_windows(TOPICS, sentences, buffer=1))
    chunks, breaks = semantic_spans(
        TOPICS, sentences, vectors, size=200, min_size=5, percentile=90, measure=Measurer(TOPICS, "tokens"),
        separators=DEFAULT_SEPARATORS,
    )  # fmt: skip
    assert breaks == [4]
    assert [TOPICS[a:b] for a, b in chunks][1].startswith("Volcanoes erupt")
    # a size limit still applies inside one long topic
    small, _ = semantic_spans(TOPICS, sentences, vectors, size=12, min_size=0, percentile=90,
                              measure=Measurer(TOPICS, "tokens"), separators=DEFAULT_SEPARATORS)  # fmt: skip
    assert all(Measurer(TOPICS, "tokens")(a, b) <= 12 for a, b in small)


def test_layout_keeps_structure_whole():
    kinds = [b.kind for b in layout_blocks(HANDBOOK)]
    assert kinds == ["heading", "paragraph", "table", "heading", "list", "code"]
    chunks = layout_chunks(HANDBOOK, 40, Measurer(HANDBOOK, "tokens"), DEFAULT_SEPARATORS)
    texts = [HANDBOOK[c.start : c.end] for c in chunks]
    assert texts[0].startswith("# Leave policy\n\nEveryone gets")  # the heading stays with its content
    assert any(t.startswith("| Type | Days |") and t.endswith("| Sick | 10 |") for t in texts)  # table not cut
    travel = next(t for t in texts if t.startswith("## Travel"))
    assert "and a business reason." in travel  # the wrapped list item stays in the list
    assert {c.section for c in chunks} == {"Leave policy", "Leave policy > Travel"}
    # an oversized table is split between rows, never inside one
    tiny = layout_chunks(HANDBOOK, 8, Measurer(HANDBOOK, "tokens"), DEFAULT_SEPARATORS)
    assert all(HANDBOOK[c.start : c.end].count("\n") == 0 or not HANDBOOK[c.start:c.end].startswith("|")
               or HANDBOOK[c.start : c.end].endswith("|") for c in tiny)  # fmt: skip


@pytest.mark.parametrize(
    "strategy,expect",
    [("sentence", 3), ("layout", 4), ("document", 1), ("semantic", 1), ("recursive", 2), ("markdown", 2)],
)
async def test_node_strategies_produce_exact_slices(strategy, expect):
    r = await invoke_node(chunker_v11, inputs={"documents": [doc(HANDBOOK)]},
                          config={"strategy": strategy, "chunk_size": 500 if strategy == "document" else 60,
                                  "chunk_overlap": 0})  # fmt: skip
    chunks = r.outputs["chunks"]
    assert len(chunks) >= 1 and (strategy != "document" or len(chunks) == expect)
    for c in chunks:
        assert HANDBOOK[c["offsets"]["start"] : c["offsets"]["end"]] == c["content"]
        assert c["metadata"]["chunk_strategy"] == strategy and c["parent_id"] == "d1"
    assert r.outputs["parents"] == []


async def test_document_strategy_splits_only_when_too_long():
    r = await invoke_node(chunker_v11, inputs={"documents": [doc("Short note."), doc(HANDBOOK, "d2")]},
                          config={"strategy": "document", "chunk_size": 30, "chunk_overlap": 0})  # fmt: skip
    by_doc = [c["parent_id"] for c in r.outputs["chunks"]]
    assert by_doc.count("d1") == 1 and by_doc.count("d2") > 1


async def test_semantic_uses_a_connected_embedding_model():
    model = {"kind": "embedding_model", "provider": "hash", "model": "hash", "dimensions": 64}
    r = await invoke_node(chunker_v11, inputs={"documents": [doc(TOPICS)], "embedding_model": model},
                          config={"strategy": "semantic", "chunk_size": 200, "min_chunk_size": 5})  # fmt: skip
    assert len(r.outputs["chunks"]) >= 2
    lexical = await invoke_node(chunker_v11, inputs={"documents": [doc(TOPICS)]},
                                config={"strategy": "semantic", "chunk_size": 200, "min_chunk_size": 5})  # fmt: skip
    assert [c["content"][:15] for c in lexical.outputs["chunks"]] == ["Cats are small ", "Volcanoes erupt"]


async def test_parent_child_and_parent_expander():
    long = " ".join(f"Sentence number {i} talks about topic {i % 7}." for i in range(120))
    r = await invoke_node(chunker_v11, inputs={"documents": [doc(long)]},
                          config={"strategy": "parent_child", "chunk_size": 40, "chunk_overlap": 0,
                                  "parent_chunk_size": 200})  # fmt: skip
    children, parents = r.outputs["chunks"], r.outputs["parents"]
    assert len(parents) >= 3 and len(children) > len(parents)
    ids = {p["id"] for p in parents}
    for c in children:
        meta = c["metadata"]
        assert meta["parent_chunk_id"] in ids
        assert meta["parent_start"] <= c["offsets"]["start"] and c["offsets"]["end"] <= meta["parent_end"]
        assert long[meta["parent_start"] : meta["parent_end"]] == meta["parent_text"]
    # retrieval returned three children: two share a parent
    p0 = [c for c in children if c["metadata"]["parent_chunk_id"] == parents[0]["id"]]
    p1 = [c for c in children if c["metadata"]["parent_chunk_id"] == parents[1]["id"]]
    retrieved = [
        {**p1[0], "retrieval_method": "vector", "score": 0.9},
        {**p0[0], "retrieval_method": "vector", "score": 0.7},
        {**p1[1], "retrieval_method": "vector", "score": 0.95},
        {**doc("unrelated", "x"), "retrieval_method": "vector", "score": 0.5},
    ]
    out = (await invoke_node(parent_expander, inputs={"documents": retrieved})).outputs["documents"]
    assert [d["id"] for d in out] == [parents[1]["id"], parents[0]["id"], "x"]
    assert out[0]["content"] == parents[1]["content"] and out[0]["score"] == 0.95
    assert (
        out[0]["metadata"]["matched_chunks"] == [p1[0]["id"], p1[1]["id"]] and "parent_text" not in out[0]["metadata"]
    )
    assert out[0]["offsets"] == parents[1]["offsets"]
    one = (await invoke_node(parent_expander, inputs={"documents": retrieved}, config={"max_parents": 1})).outputs
    assert [d["id"] for d in one["documents"]] == [parents[1]["id"], "x"]


def test_config_rules_and_old_version_unchanged():
    with pytest.raises(ValueError, match="parent_chunk_size"):
        ChunkerConfigV11(strategy="parent_child", chunk_size=300, parent_chunk_size=200)
    assert chunker.manifest.version == "1.0.0" and chunker_v11.manifest.version == "1.1.0"
    schema = chunker.manifest.config_schema["properties"]["strategy"]
    assert schema["enum"] == ["recursive", "fixed", "markdown"]  # 1.0.0 keeps its contract
    shown = chunker_v11.manifest.config_schema["properties"]["parent_chunk_size"]["x-ui"]["show_when"]
    assert shown == {"strategy": ["parent_child"]}

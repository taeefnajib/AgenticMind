import pytest
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_rag.answering import ContextBuilderConfig, citation_builder, context_builder
from pydantic import ValidationError


def doc(i, content, score, parent="art_1", start=0, end=None, page=None, section=None):
    source = {"uri": f"artifact://{parent}/h.md", "filename": "h.md"}
    if page:
        source["page"] = page
    if section:
        source["section"] = section
    return {
        "id": f"{parent}#c{i}",
        "content": content,
        "source": source,
        "parent_id": parent,
        "offsets": {"start": start, "end": end if end is not None else start + len(content)},
        "retrieval_method": "vector",
        "score": score,
    }


DOCS = [
    doc(0, "Employees receive 25 days of annual leave.", 0.9, start=0, section="Leave"),
    doc(1, "Employees receive 25 days of annual leave.", 0.8, start=500),  # exact duplicate text
    doc(2, "Hotels are reimbursed up to 180 EUR per night.", 0.7, start=100, page=2),
    doc(3, "Report phishing within one hour.", 0.6, start=900),
]


async def test_context_builder_numbers_sources_and_builds_prompt():
    r = await invoke_node(context_builder, inputs={"documents": DOCS, "question": "How much leave?"})
    sources = r.outputs["sources"]
    assert [s["n"] for s in sources] == [1, 2, 3]  # duplicate dropped
    assert sources[0]["section"] == "Leave" and sources[0]["score"] == 0.9 and sources[1]["page"] == 2
    assert r.outputs["context"].startswith("[1] h.md · Leave\nEmployees receive 25 days")
    assert "[2] h.md · p. 2\nHotels" in r.outputs["context"]
    assert r.outputs["prompt"].endswith("Question: How much leave?")
    assert "{{" not in r.outputs["prompt"]


async def test_context_builder_budget_and_order():
    long_docs = [
        doc(i, f"Fact {i}. " + "word " * 60, 0.9 - i / 10, start=i * 1000) for i in range(3)
    ]  # ~70 tokens each
    r = await invoke_node(
        context_builder, inputs={"documents": long_docs, "question": "q"}, config={"token_budget": 100}
    )
    assert len(r.outputs["sources"]) == 1  # only the best source fits
    huge = [doc(0, "word " * 400, 0.9)]
    r = await invoke_node(context_builder, inputs={"documents": huge, "question": "q"}, config={"token_budget": 50})
    assert len(r.outputs["sources"]) == 1 and r.outputs["context"].endswith("…")  # truncated, never empty
    r = await invoke_node(
        context_builder, inputs={"documents": DOCS, "question": "q"}, config={"order": "document", "deduplicate": False}
    )
    # reading order: page first, then position (c2 is on page 2)
    assert [s["id"] for s in r.outputs["sources"]] == ["art_1#c0", "art_1#c1", "art_1#c3", "art_1#c2"]


async def test_context_builder_handles_no_documents():
    r = await invoke_node(context_builder, inputs={"documents": [], "question": "q"})
    assert r.outputs["sources"] == [] and "no sources" in r.outputs["context"]


def test_instructions_need_placeholders():
    with pytest.raises(ValidationError):
        ContextBuilderConfig(instructions="Answer: {{question}}")


SOURCES = [{"n": 1, "id": "a"}, {"n": 2, "id": "b"}, {"n": 3, "id": "c"}]


async def test_citations_are_linked_and_validated():
    r = await invoke_node(
        citation_builder,
        inputs={"answer": "Leave is 25 days [1][3]. Hotels [1-2] are covered [9].", "sources": SOURCES},
    )
    v = r.outputs["result"]["validation"]
    assert v == {"has_citations": True, "cited": [1, 3, 2], "invalid": [9], "uncited": []}
    assert [c["id"] for c in r.outputs["citations"]] == ["a", "c", "b"]
    assert r.outputs["answer"] == "Leave is 25 days [1][3]. Hotels [1][2] are covered."


async def test_citations_keep_invalid_when_asked_and_require():
    r = await invoke_node(
        citation_builder, inputs={"answer": "See [4].", "sources": SOURCES}, config={"remove_invalid": False}
    )
    assert r.outputs["answer"] == "See [4]." and r.outputs["result"]["validation"]["has_citations"] is False
    with pytest.raises(NodeError) as exc:
        await invoke_node(
            citation_builder, inputs={"answer": "No citations.", "sources": SOURCES}, config={"require_citations": True}
        )
    assert exc.value.code == "CITATIONS_MISSING"
    with pytest.raises(NodeError):
        await invoke_node(citation_builder, inputs={"answer": "x", "sources": [{"id": "no-n"}]})

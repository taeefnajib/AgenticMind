import itertools

import pytest
from agenticmind_node_sdk.documents import parse_documents
from agenticmind_node_sdk.testing import invoke_node
from agenticmind_nodes_rag.chunker import ChunkerConfig, chunker
from agenticmind_nodes_rag.chunking import Measurer, chunk_text, markdown_sections
from pydantic import ValidationError

TEXT = (
    "# Handbook\n\nWelcome to the company. This handbook explains our policies in detail.\n\n"
    "## Leave\n\nEmployees receive 25 days of annual leave. Unused leave carries over for one year! "
    "Requests go through the HR portal; managers approve within five working days.\n\n"
    "## Expenses\n\nSubmit receipts within 30 days. Travel must be pre-approved, and economy class is default. "
    + "Lorem ipsum dolor sit amet consectetur. " * 12
    + "\n\nVeryLongUnbrokenTokenWithoutAnySpaces" * 3
)


@pytest.mark.parametrize(
    "strategy,unit,size,overlap",
    list(itertools.product(["recursive", "fixed", "markdown"], ["tokens", "characters"], [20, 64], [0, 8])),
)
def test_chunks_are_exact_bounded_and_cover_text(strategy, unit, size, overlap):
    chunks = chunk_text(TEXT, strategy=strategy, unit=unit, size=size, overlap=overlap)
    measure = Measurer(TEXT, unit)
    covered = set()
    for c in chunks:
        piece = TEXT[c.start : c.end]
        assert piece == piece.strip() and piece  # stripped, non-empty
        assert measure(c.start, c.end) <= size, (piece, measure(c.start, c.end))
        covered.update(range(c.start, c.end))
    missing = [i for i, ch in enumerate(TEXT) if not ch.isspace() and i not in covered]
    assert not missing, f"uncovered text: {TEXT[missing[0] : missing[0] + 30]!r}"


def test_overlap_shares_content():
    chunks = chunk_text(TEXT, strategy="fixed", unit="characters", size=100, overlap=30)
    assert any(b.start < a.end for a, b in itertools.pairwise(chunks))
    no_overlap = chunk_text(TEXT, strategy="fixed", unit="characters", size=100, overlap=0)
    assert all(b.start >= a.end for a, b in itertools.pairwise(no_overlap))


def test_recursive_prefers_paragraph_boundaries():
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird one."
    chunks = chunk_text(text, strategy="recursive", unit="tokens", size=5, overlap=0)
    assert [text[c.start : c.end] for c in chunks] == ["First paragraph here.", "Second paragraph here.", "Third one."]


def test_markdown_sections_have_heading_paths():
    sections = markdown_sections(TEXT)
    assert [s[2] for s in sections] == ["Handbook", "Handbook > Leave", "Handbook > Expenses"]
    chunks = chunk_text(TEXT, strategy="markdown", unit="tokens", size=40, overlap=0)
    leave = [TEXT[c.start : c.end] for c in chunks if c.section == "Handbook > Leave"]
    assert leave and all("Expenses" not in p for p in leave)


def test_token_measure():
    m = Measurer("Hello, world! Bye.", "tokens")
    assert m(0, 18) == 6  # Hello , world ! Bye .


def test_invalid_overlap():
    with pytest.raises(ValidationError):
        ChunkerConfig(chunk_size=10, chunk_overlap=10)
    with pytest.raises(ValueError):
        chunk_text("x", strategy="fixed", unit="tokens", size=5, overlap=5)


async def test_chunker_node_preserves_provenance():
    doc = {
        "id": "art_1:p2",
        "content": TEXT,
        "source": {"uri": "artifact://art_1/h.pdf", "filename": "h.pdf", "page": 2},
        "metadata": {"sha256": "abc"},
        "ingestion_version": "loader@1",
    }
    result = await invoke_node(
        chunker, inputs={"documents": [doc]}, config={"strategy": "markdown", "chunk_size": 40, "chunk_overlap": 5}
    )
    chunks = parse_documents(result.outputs["chunks"])
    assert len(chunks) > 3
    for i, c in enumerate(chunks):
        assert c.id == f"art_1:p2#c{i}" and c.parent_id == "art_1:p2"
        assert TEXT[c.offsets.start : c.offsets.end] == c.content
        assert c.source.page == 2 and c.metadata["sha256"] == "abc"
        assert c.metadata["chunk_count"] == len(chunks)
        assert c.ingestion_version == "loader@1 > agenticmind.rag.chunker@1.0.0"
    assert chunks[-1].source.section == "Handbook > Expenses"

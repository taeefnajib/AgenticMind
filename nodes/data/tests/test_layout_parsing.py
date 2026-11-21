"""Layout-aware parsing (ADR-0027): structure survives parsing so layout chunking can use it."""

from __future__ import annotations

import io

from agenticmind_node_sdk.testing import MemoryArtifacts, invoke_node
from agenticmind_nodes_data.nodes import document_loader
from agenticmind_nodes_data.parsers import parse_bytes
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def make_pdf(lines: list[tuple[int, int, str]]) -> bytes:
    """A one-page PDF drawing each (font size, y, text) line in Helvetica."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })  # fmt: skip
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    stream = DecodedStreamObject()
    stream.set_data("".join(f"BT /F1 {size} Tf 72 {y} Td ({text}) Tj ET\n" for size, y, text in lines).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


POLICY = make_pdf([
    (24, 720, "Leave policy"), (11, 690, "Everyone gets 25 days of annual leave."),
    (11, 676, "Ask your manager first."), (16, 640, "Sick leave"), (11, 615, "Ten days per year."),
    (11, 560, "Doctor notes are needed after three days."),
])  # fmt: skip


def test_pdf_layout_turns_larger_fonts_into_headings():
    (part,) = parse_bytes(POLICY, "policy.pdf", pdf_layout=True)
    assert part.content == (
        "# Leave policy\n\nEveryone gets 25 days of annual leave.\nAsk your manager first.\n\n## Sick leave\n\n"
        "Ten days per year.\n\nDoctor notes are needed after three days."  # the vertical gap starts a paragraph
    )
    assert part.page == 1 and part.metadata["layout"] == "font-size headings"
    (plain,) = parse_bytes(POLICY, "policy.pdf")
    assert "#" not in plain.content and plain.content.startswith("Leave policy")


async def test_loader_option_feeds_layout_chunking():
    from agenticmind_nodes_rag.chunker import chunker_v11

    files = MemoryArtifacts({"p": ("policy.pdf", POLICY)})
    docs = (await invoke_node(document_loader, config={"files": ["p"], "pdf_layout": True}, artifacts=files)).outputs
    chunks = (
        await invoke_node(
            chunker_v11, inputs={"documents": docs["documents"]}, config={"strategy": "layout", "chunk_size": 200}
        )  # fmt: skip
    ).outputs["chunks"]
    assert [c["source"]["section"] for c in chunks] == ["Leave policy", "Leave policy > Sick leave"]
    assert chunks[1]["content"].startswith("## Sick leave") and chunks[1]["source"]["page"] == 1


def test_html_lists_tables_and_code_keep_structure():
    html = (
        b"<h2>Setup</h2><ol><li>Install</li><li>Run</li></ol><table><tr><th>Key</th><th>Value</th></tr>"
        b"<tr><td>port</td><td>8080</td></tr></table><pre>server:\n    port: 8080</pre><p>Done.</p>"
    )
    (part,) = parse_bytes(html, "setup.html")
    assert part.content == (
        "## Setup\n\n- Install\n- Run\n\n| Key | Value |\n| port | 8080 |\n\n```\nserver:\n    port: 8080\n```\n\nDone."
    )


def test_docx_tables_become_markdown_rows():
    import docx

    d = docx.Document()
    d.add_heading("Rates", level=1)
    table = d.add_table(rows=2, cols=2)
    for r, (a, b) in enumerate([("Region", "Rate"), ("EU", "21%")]):
        table.cell(r, 0).text, table.cell(r, 1).text = a, b
    buf = io.BytesIO()
    d.save(buf)
    (part,) = parse_bytes(buf.getvalue(), "rates.docx")
    assert part.content == "# Rates\n\n| Region | Rate |\n| EU | 21% |"

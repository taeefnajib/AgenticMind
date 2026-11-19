# ruff: noqa: E501  (inline OOXML/EPUB fixtures read better on one line)
"""XLSX, PPTX, EPUB, JSONL, TSV and text formats (built in memory, standard OOXML/EPUB layout)."""

from __future__ import annotations

import io
import zipfile

import pytest
from agenticmind_nodes_data import parsers
from agenticmind_nodes_data.parsers import ParseError, parse_bytes

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def zipped(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def rels(*items: tuple[str, str]) -> str:
    body = "".join(f'<Relationship Id="{i}" Type="x" Target="{t}"/>' for i, t in items)
    return f'<Relationships xmlns="{PKG}">{body}</Relationships>'


def xlsx() -> bytes:
    sheet1 = (
        f'<worksheet xmlns="{SS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="inlineStr"><is><t>Active</t></is></c></row>'
        '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1250.5</v></c><c r="C2" t="b"><v>1</v></c></row>'
        '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="C3" t="b"><v>0</v></c></row>'
        "</sheetData></worksheet>"
    )  # B3 missing: sparse cells keep their columns
    sheet2 = f'<worksheet xmlns="{SS}"><sheetData><row r="1"><c r="B1" t="inlineStr"><is><t>Only</t></is></c></row></sheetData></worksheet>'
    return zipped(
        {
            "xl/workbook.xml": f'<workbook xmlns="{SS}" xmlns:r="{R}"><sheets>'
            '<sheet name="Customers" sheetId="1" r:id="rId1"/><sheet name="Notes" sheetId="2" r:id="rId2"/>'
            "</sheets></workbook>",
            "xl/_rels/workbook.xml.rels": rels(
                ("rId1", "worksheets/sheet1.xml"), ("rId2", "/xl/worksheets/sheet2.xml")
            ),
            "xl/sharedStrings.xml": f'<sst xmlns="{SS}"><si><t>Name</t></si><si><t>Revenue</t></si>'
            "<si><t>Acme</t></si><si><r><t>Glo</t></r><r><t>bex</t></r></si></sst>",
            "xl/worksheets/sheet1.xml": sheet1,
            "xl/worksheets/sheet2.xml": sheet2,
        }
    )


def test_xlsx_sheets_rows_and_cell_types():
    parts = parse_bytes(xlsx(), "sales.xlsx")
    assert [p.section for p in parts] == ["Customers", "Notes"]
    assert parts[0].content == "Name: Acme\nRevenue: 1250.5\nActive: TRUE\n\nName: Globex\nActive: FALSE"
    assert parts[0].metadata == {"sheet": "Customers", "row_count": 2, "columns": ["Name", "Revenue", "Active"]}
    assert parts[1].metadata["columns"] == ["column 1", "Only"]  # empty header cell gets a name
    rows = parse_bytes(xlsx(), "sales.xlsx", csv_mode="rows")
    assert [p.section for p in rows] == ["Customers row 1", "Customers row 2"]
    assert len({p.id_suffix for p in rows}) == 2


def pptx() -> bytes:
    def slide(*paras: str) -> str:
        ps = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paras)
        return f'<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>{ps}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'

    return zipped(
        {
            "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>'
            '<p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>',
            "ppt/_rels/presentation.xml.rels": rels(("rId2", "slides/slide1.xml"), ("rId3", "slides/slide2.xml")),
            "ppt/slides/slide1.xml": slide("Results", "Revenue up 12%"),
            "ppt/slides/slide2.xml": slide("Agenda", "Intro", "Results"),
            "ppt/slides/_rels/slide1.xml.rels": rels(("rId1", "../notesSlides/notesSlide1.xml")),
            "ppt/notesSlides/notesSlide1.xml": slide("Mention the new region", "2"),
        }
    )


def test_pptx_slides_follow_presentation_order_with_notes():
    parts = parse_bytes(pptx(), "deck.pptx")
    assert [(p.page, p.section) for p in parts] == [(1, "slide 1"), (2, "slide 2")]
    assert parts[0].content == "## Agenda\n\nIntro\nResults"  # slide2.xml is listed first
    assert parts[1].content == "## Results\n\nRevenue up 12%\n\nSpeaker notes:\nMention the new region"


def test_epub_chapters_in_spine_order():
    data = zipped(
        {
            "META-INF/container.xml": '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
            "OEBPS/content.opf": '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<metadata><dc:title>Field Guide</dc:title></metadata>"
            '<manifest><item id="c1" href="text/one.xhtml"/><item id="c2" href="text/two.xhtml"/></manifest>'
            '<spine><itemref idref="c2"/><itemref idref="c1"/></spine></package>',
            "OEBPS/text/one.xhtml": "<html><body><h1>Owls</h1><p>Hunt at night.</p></body></html>",
            "OEBPS/text/two.xhtml": "<html><head><style>p{}</style></head><body><h1>Intro</h1><p>Welcome.</p></body></html>",
        }
    )
    parts = parse_bytes(data, "guide.epub")
    assert [p.section for p in parts] == ["chapter 1", "chapter 2"]
    assert "Intro" in parts[0].content and "Welcome." in parts[0].content and "p{}" not in parts[0].content
    assert "Owls" in parts[1].content and parts[1].metadata == {"title": "Field Guide"}


def test_jsonl_tsv_and_text_formats():
    jsonl = b'{"q": "a"}\n\n{"q": "b"}\n'
    assert parse_bytes(jsonl, "d.jsonl")[0].metadata == {"record_count": 2}
    assert [p.section for p in parse_bytes(jsonl, "d.ndjson", csv_mode="rows")] == ["record 1", "record 2"]
    with pytest.raises(ParseError, match="line 2"):
        parse_bytes(b'{"q": 1}\n{oops\n', "bad.jsonl")
    tsv = parse_bytes(b"name\tcity\nAda\tLondon\n", "people.tsv")[0]
    assert tsv.content == "name: Ada\ncity: London"
    for ext in (".yaml", ".yml", ".rst", ".log", ".xml"):
        assert parse_bytes(b"key: value\r\n", f"f{ext}")[0].content == "key: value\n"


def test_broken_and_oversized_containers_are_refused(monkeypatch):
    with pytest.raises(ParseError, match="not a zip"):
        parse_bytes(b"not a zip", "x.xlsx")
    with pytest.raises(ParseError, match="missing xl/workbook.xml"):
        parse_bytes(zipped({"other.txt": "x"}), "x.xlsx")
    monkeypatch.setattr(parsers, "MAX_MEMBER_BYTES", 100)
    big = zipped({"xl/workbook.xml": "<workbook>" + " " * 500 + "</workbook>"})
    with pytest.raises(ParseError, match="too large"):
        parse_bytes(big, "bomb.xlsx")

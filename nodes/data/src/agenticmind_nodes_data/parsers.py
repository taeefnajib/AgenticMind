"""Format parsers: bytes → text parts. Pure functions, no I/O beyond the given bytes.

Structure is preserved as lightweight Markdown where the source has it (DOCX/HTML headings become
``#`` lines) so the structure-aware chunker can use it downstream.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class ParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ParsedPart:
    content: str
    page: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id_suffix: str = ""


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# -- PDF ----------------------------------------------------------------------------------------
def parse_pdf(data: bytes, *, split_pages: bool) -> list[ParsedPart]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("PARSE_ENCRYPTED", "PDF is password-protected")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except PdfReadError as exc:
        raise ParseError("PARSE_FAILED", f"Not a readable PDF: {exc}") from exc
    total = len(pages)
    if split_pages:
        return [
            ParsedPart(content=text, page=i, metadata={"page_count": total}, id_suffix=f":p{i}")
            for i, text in enumerate(pages, start=1)
        ]
    return [ParsedPart(content="\n\n".join(p for p in pages if p), metadata={"page_count": total})]


# -- DOCX ---------------------------------------------------------------------------------------
_HEADING_STYLE = re.compile(r"^Heading (\d)$")


def parse_docx(data: bytes) -> list[ParsedPart]:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - python-docx raises several unrelated types
        raise ParseError("PARSE_FAILED", f"Not a readable DOCX file: {exc}") from exc
    blocks: list[str] = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = para.style.name if para.style is not None else ""
            m = _HEADING_STYLE.match(style)
            if style == "Title":
                text = f"# {text}"
            elif m:
                text = f"{'#' * min(int(m.group(1)), 6)} {text}"
            blocks.append(text)
        elif tag == "tbl":
            table = Table(child, document)
            rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
            if rows:
                blocks.append("\n".join(rows))
    return [ParsedPart(content="\n\n".join(blocks))]


# -- HTML ---------------------------------------------------------------------------------------
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "li",
    "ul",
    "ol",
    "br",
    "tr",
    "table",
    "blockquote",
    "pre",
    "main",
    "aside",
    "nav",
    "dd",
    "dt",
    "figure",
    "figcaption",
}
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data.strip()
        if self.skip_depth == 0:
            self.parts.append(data)


def parse_html(data: bytes) -> list[ParsedPart]:
    extractor = _TextExtractor()
    extractor.feed(decode_text(data))
    extractor.close()
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    meta = {"title": extractor.title} if extractor.title else {}
    return [ParsedPart(content=text, metadata=meta)]


# -- CSV / JSON / text -------------------------------------------------------------------------
def parse_csv(data: bytes, *, mode: str) -> list[ParsedPart]:
    text = decode_text(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return [ParsedPart(content="")]
    header, body = rows[0], rows[1:]

    def render(row: list[str]) -> str:
        return "\n".join(f"{h}: {v}" for h, v in zip(header, row, strict=False) if str(v).strip())

    if mode == "rows":
        return [
            ParsedPart(
                content=render(r), section=f"row {i}", metadata={"row": i, "columns": header}, id_suffix=f":r{i}"
            )
            for i, r in enumerate(body, start=1)
        ]
    return [
        ParsedPart(
            content="\n\n".join(render(r) for r in body),
            metadata={"row_count": len(body), "columns": header},
        )
    ]


def parse_json(data: bytes) -> list[ParsedPart]:
    try:
        value = json.loads(decode_text(data))
    except json.JSONDecodeError as exc:
        raise ParseError("PARSE_FAILED", f"Invalid JSON: {exc}") from exc
    return [ParsedPart(content=json.dumps(value, indent=2, ensure_ascii=False))]


def parse_bytes(
    data: bytes, filename: str, *, split_pdf_pages: bool = True, csv_mode: str = "file"
) -> list[ParsedPart]:
    ext = Path(filename).suffix.lower()
    match ext:
        case ".pdf":
            return parse_pdf(data, split_pages=split_pdf_pages)
        case ".docx":
            return parse_docx(data)
        case ".html" | ".htm":
            return parse_html(data)
        case ".csv":
            return parse_csv(data, mode=csv_mode)
        case ".json":
            return parse_json(data)
        case ".txt" | ".md" | ".markdown":
            return [ParsedPart(content=decode_text(data).replace("\r\n", "\n"))]
    raise ParseError("PARSE_UNSUPPORTED", f"Unsupported file type '{ext}'")

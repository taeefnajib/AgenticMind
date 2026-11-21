"""Format parsers: bytes → text parts. Pure functions, no I/O beyond the given bytes.

Structure is preserved as lightweight Markdown where the source has it (DOCX/HTML headings become
``#`` lines) so the structure-aware chunker can use it downstream.
"""

from __future__ import annotations

import csv
import io
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


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


def parse_pdf_layout(data: bytes, *, split_pages: bool) -> list[ParsedPart]:
    """Layout-aware PDF parsing: lines are rebuilt from text runs, and lines set in a noticeably larger font
    than the body text become Markdown headings (largest size = ``#``), so heading-aware chunking can use them.
    Paragraph breaks come from vertical gaps. Tables are not detected in PDFs."""
    from collections import Counter

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("PARSE_ENCRYPTED", "PDF is password-protected")
        pages_lines: list[list[tuple[float, float, str]]] = []  # (y, size, text) per line
        for page in reader.pages:
            runs: list[tuple[float, float, str]] = []

            def visit(text: str, cm: Any, tm: Any, font: Any, size: float, runs: list = runs) -> None:
                if text.strip():
                    scale = abs(tm[3] * cm[3]) or abs(tm[0] * cm[0]) or 1.0
                    runs.append((round(tm[5] * cm[3] + cm[5], 1), round(size * scale, 1), text))

            page.extract_text(visitor_text=visit)
            lines: list[tuple[float, float, str]] = []
            for y, size, text in runs:
                if lines and abs(lines[-1][0] - y) <= max(2.0, size * 0.3):
                    py, psize, ptext = lines[-1]
                    lines[-1] = (py, max(psize, size), ptext + text)
                else:
                    lines.append((y, size, text))
            pages_lines.append([(y, s, " ".join(t.split())) for y, s, t in lines if t.strip()])
    except PdfReadError as exc:
        raise ParseError("PARSE_FAILED", f"Not a readable PDF: {exc}") from exc
    weights: Counter[float] = Counter()
    for lines in pages_lines:
        for _, size, text in lines:
            weights[size] += len(text)
    body = weights.most_common(1)[0][0] if weights else 0.0
    heading_sizes = sorted(
        {s for lines in pages_lines for _, s, t in lines if body and s >= body * 1.15 and len(t) <= 150}, reverse=True
    )[:6]
    level = {s: i + 1 for i, s in enumerate(heading_sizes)}

    def render(lines: list[tuple[float, float, str]]) -> str:
        out: list[str] = []
        prev_y: float | None = None
        prev_size = body
        for y, size, text in lines:
            if size in level:
                out.append("")
                out.append("#" * level[size] + " " + text)
                out.append("")
            else:
                gap = abs(prev_y - y) if prev_y is not None else 0
                if out and out[-1] and gap > max(prev_size, size) * 1.8:
                    out.append("")
                out.append(text)
            prev_y, prev_size = y, size
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

    pages = [render(lines) for lines in pages_lines]
    total = len(pages)
    meta = {"page_count": total, "layout": "font-size headings"}
    if split_pages:
        return [ParsedPart(content=text, page=i, metadata=meta, id_suffix=f":p{i}") for i, text in enumerate(pages, 1)]
    return [ParsedPart(content="\n\n".join(p for p in pages if p), metadata=meta)]


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
            rows = ["| " + " | ".join(cell.text.strip() for cell in row.cells) + " |" for row in table.rows]
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
        self._pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "tr":
            self.parts.append("\n|")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag == "pre":
            self.parts.append("\n\n```\n")
            self._pre += 1
        elif tag == "table":
            self.parts.append("\n\n")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in ("td", "th"):
            self.parts.append(" |")
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self.parts.append("\n```\n\n")
            return
        if tag in ("li", "tr"):
            return
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data.strip()
        if self.skip_depth == 0:
            # inside <pre> keep line breaks (marked, restored after whitespace is normalised)
            pre = data.replace("\n", "\x00").replace("\t", "    ").replace(" ", "\x01")
            self.parts.append(pre if self._pre else data)


def parse_html(data: bytes) -> list[ParsedPart]:
    extractor = _TextExtractor()
    extractor.feed(decode_text(data))
    extractor.close()
    text = "".join(extractor.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip().replace("\x00", "\n").replace("\x01", " ")
    text = re.sub(r"\n- \n", "\n- ", text)  # list items whose text started on a new line
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


# -- JSON Lines ---------------------------------------------------------------------------------
def parse_jsonl(data: bytes, *, mode: str) -> list[ParsedPart]:
    records: list[str] = []
    for n, line in enumerate(decode_text(data).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ParseError("PARSE_FAILED", f"Invalid JSON on line {n}: {exc}") from exc
        records.append(json.dumps(value, indent=2, ensure_ascii=False))
    if mode == "rows":
        return [
            ParsedPart(content=r, section=f"record {i}", metadata={"record": i}, id_suffix=f":r{i}")
            for i, r in enumerate(records, start=1)
        ]
    return [ParsedPart(content="\n\n".join(records), metadata={"record_count": len(records)})]


# -- Office Open XML / EPUB (zip containers, read with the standard library) ---------------------
#: Guards against zip bombs: per-member and total uncompressed bytes read from one file.
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 5000


class _Zip:
    def __init__(self, data: bytes, kind: str) -> None:
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ParseError("PARSE_FAILED", f"Not a readable {kind} file (not a zip container)") from exc
        if len(self.zf.infolist()) > MAX_MEMBERS:
            raise ParseError("PARSE_FAILED", f"{kind} file has too many parts")
        self.kind = kind
        self.total = 0
        self.names = set(self.zf.namelist())

    def read(self, name: str) -> bytes:
        try:
            info = self.zf.getinfo(name)
        except KeyError as exc:
            raise ParseError("PARSE_FAILED", f"{self.kind} file is missing {name}") from exc
        if info.file_size > MAX_MEMBER_BYTES or self.total + info.file_size > MAX_TOTAL_BYTES:
            raise ParseError("PARSE_FAILED", f"{self.kind} file is too large when uncompressed")
        with self.zf.open(info) as fh:
            raw = fh.read(MAX_MEMBER_BYTES + 1)  # the declared size can lie
        if len(raw) > MAX_MEMBER_BYTES:
            raise ParseError("PARSE_FAILED", f"{self.kind} file is too large when uncompressed")
        self.total += len(raw)
        return raw

    def xml(self, name: str) -> ET.Element:
        try:
            return ET.fromstring(self.read(name))
        except ET.ParseError as exc:
            raise ParseError("PARSE_FAILED", f"{self.kind} part {name} is not valid XML: {exc}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(el: ET.Element, local_name: str, *, namespaced: bool = True) -> str:
    for k, v in el.attrib.items():
        if _local(k) == local_name and (not namespaced or k.startswith("{")):
            return v
    return ""


def _texts(el: ET.Element) -> str:
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _rels(z: _Zip, part: str) -> dict[str, str]:
    """Relationship id -> target path (resolved against ``part``'s folder)."""
    folder, name = posixpath.split(part)
    rels_name = posixpath.join(folder, "_rels", f"{name}.rels")
    if rels_name not in z.names:
        return {}
    out = {}
    for rel in z.xml(rels_name):
        target = rel.get("Target", "")
        resolved = target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target)
        out[rel.get("Id", "")] = posixpath.normpath(resolved)
    return out


_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(ref: str) -> int:
    m = _CELL_REF.match(ref or "")
    if not m:
        return -1
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _sheet_rows(root: ET.Element, shared: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in (e for e in root.iter() if _local(e.tag) == "row"):
        cells: dict[int, str] = {}
        for c in (e for e in row if _local(e.tag) == "c"):
            kind = c.get("t", "n")
            if kind == "inlineStr":
                value = _texts(c)
            else:
                v = next((e.text for e in c if _local(e.tag) == "v"), None) or ""
                if kind == "s":
                    value = shared[int(v)] if v.isdigit() and int(v) < len(shared) else ""
                elif kind == "b":
                    value = "TRUE" if v == "1" else "FALSE"
                else:
                    value = v
            idx = _col_index(c.get("r", ""))
            cells[idx if idx >= 0 else len(cells)] = value
        if any(v.strip() for v in cells.values()):
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    return rows


def parse_xlsx(data: bytes, *, mode: str) -> list[ParsedPart]:
    """Each worksheet becomes a part (one part per row with ``mode="rows"``); the first row is the header.

    Cached values are read as stored: formulas give their last computed value, dates their serial number.
    """
    z = _Zip(data, "XLSX")
    shared = [_texts(si) for si in z.xml("xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in z.names else []
    rels = _rels(z, "xl/workbook.xml")
    parts: list[ParsedPart] = []
    for sheet in (e for e in z.xml("xl/workbook.xml").iter() if _local(e.tag) == "sheet"):
        name = sheet.get("name", "Sheet")
        target = rels.get(_attr(sheet, "id"))
        if not target or target not in z.names:
            continue
        rows = _sheet_rows(z.xml(target), shared)
        if not rows:
            continue
        header = [h or f"column {i + 1}" for i, h in enumerate(rows[0])]
        body = rows[1:]

        def render(r: list[str], header: list[str] = header) -> str:
            return "\n".join(f"{h}: {v}" for h, v in zip(header, r, strict=False) if str(v).strip())

        if mode == "rows":
            start = len(parts)
            parts.extend(
                ParsedPart(
                    content=render(r),
                    section=f"{name} row {i}",
                    metadata={"sheet": name, "row": i, "columns": header},
                    id_suffix=f":x{start + i}",
                )
                for i, r in enumerate(body, start=1)
            )
        else:
            parts.append(
                ParsedPart(
                    content="\n\n".join(render(r) for r in body),
                    section=name,
                    metadata={"sheet": name, "row_count": len(body), "columns": header},
                    id_suffix=f":s{len(parts) + 1}",
                )
            )
    return parts or [ParsedPart(content="")]


_SLIDE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _paragraphs(root: ET.Element) -> list[str]:
    out = []
    for p in (e for e in root.iter() if _local(e.tag) == "p"):
        text = _texts(p).strip()
        if text:
            out.append(text)
    return out


def parse_pptx(data: bytes) -> list[ParsedPart]:
    """One part per slide in presentation order (the first line is its title), speaker notes included."""
    z = _Zip(data, "PPTX")
    order: list[str] = []
    if "ppt/presentation.xml" in z.names:
        rels = _rels(z, "ppt/presentation.xml")
        for sid in (e for e in z.xml("ppt/presentation.xml").iter() if _local(e.tag) == "sldId"):
            target = rels.get(_attr(sid, "id"))
            if target in z.names:
                order.append(target)
    if not order:
        slides = [n for n in z.names if _SLIDE.match(n)]
        order = sorted(slides, key=lambda n: int(_SLIDE.match(n).group(1)))  # type: ignore[union-attr]
    parts = []
    for number, slide in enumerate(order, start=1):
        lines = _paragraphs(z.xml(slide))
        notes = [t for t in _rels(z, slide).values() if t.startswith("ppt/notesSlides/") and t in z.names]
        # notes slides repeat the slide number as a placeholder: drop bare numbers
        note_lines = [line for n in notes for line in _paragraphs(z.xml(n)) if not line.isdigit()]
        if not lines and not note_lines:
            continue
        body = (f"## {lines[0]}\n\n" + "\n".join(lines[1:])) if lines else ""
        if note_lines:
            body += "\n\nSpeaker notes:\n" + "\n".join(note_lines)
        parts.append(ParsedPart(content=body.strip(), page=number, section=f"slide {number}", id_suffix=f":p{number}"))
    return parts or [ParsedPart(content="")]


def parse_epub(data: bytes) -> list[ParsedPart]:
    """One part per chapter, in reading (spine) order."""
    z = _Zip(data, "EPUB")
    container = z.xml("META-INF/container.xml")
    rootfile = next((e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile"), None)
    if not rootfile:
        raise ParseError("PARSE_FAILED", "EPUB has no package document")
    opf = z.xml(rootfile)
    folder = posixpath.dirname(rootfile)
    manifest = {
        e.get("id"): posixpath.normpath(posixpath.join(folder, e.get("href", "")))
        for e in opf.iter()
        if _local(e.tag) == "item"
    }
    title = next((e.text for e in opf.iter() if _local(e.tag) == "title" and e.text), None)
    parts = []
    for ref in (e for e in opf.iter() if _local(e.tag) == "itemref"):
        path = manifest.get(ref.get("idref"))
        if not path or path not in z.names:
            continue
        text = "\n\n".join(p.content for p in parse_html(z.read(path))).strip()
        if text:
            n = len(parts) + 1
            meta = {"title": title} if title else {}
            parts.append(ParsedPart(content=text, section=f"chapter {n}", metadata=meta, id_suffix=f":c{n}"))
    return parts or [ParsedPart(content="")]


#: Formats read as plain text.
TEXT_EXTENSIONS = (".txt", ".md", ".markdown", ".rst", ".log", ".yaml", ".yml", ".xml")
#: Every extension the loaders can turn into text (images and scanned PDFs via OCR).
SUPPORTED_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".html", ".htm", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xlsx", ".pptx", ".epub",
     *TEXT_EXTENSIONS, ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
)  # fmt: skip


def parse_bytes(
    data: bytes, filename: str, *, split_pdf_pages: bool = True, csv_mode: str = "file", pdf_layout: bool = False
) -> list[ParsedPart]:
    ext = Path(filename).suffix.lower()
    match ext:
        case ".pdf":
            if pdf_layout:
                return parse_pdf_layout(data, split_pages=split_pdf_pages)
            return parse_pdf(data, split_pages=split_pdf_pages)
        case ".docx":
            return parse_docx(data)
        case ".html" | ".htm":
            return parse_html(data)
        case ".csv" | ".tsv":
            return parse_csv(data, mode=csv_mode)
        case ".json":
            return parse_json(data)
        case ".jsonl" | ".ndjson":
            return parse_jsonl(data, mode=csv_mode)
        case ".xlsx":
            return parse_xlsx(data, mode=csv_mode)
        case ".pptx":
            return parse_pptx(data)
        case ".epub":
            return parse_epub(data)
        case _ if ext in TEXT_EXTENSIONS:
            return [ParsedPart(content=decode_text(data).replace("\r\n", "\n"))]
    raise ParseError("PARSE_UNSUPPORTED", f"Unsupported file type '{ext}'")

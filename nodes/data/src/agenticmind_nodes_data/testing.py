"""Test fixtures: tiny, valid documents generated in code (no binary files in the repo)."""

from __future__ import annotations

import io


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_test_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF with one text page per entry (lines split on ``\\n``)."""
    objects: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids: list[int] = []
    for text in pages:
        ops = " ".join(f"({_pdf_escape(line)}) Tj T*" for line in text.split("\n"))
        stream = f"BT /F1 12 Tf 14 TL 72 720 Td {ops} ET".encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        kids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_test_docx(blocks: list[tuple[str, str]]) -> bytes:
    """Build a DOCX from ``(style, text)`` pairs, e.g. ``("Heading 1", "Intro"), ("Normal", "...")``."""
    import docx

    document = docx.Document()
    for style, text in blocks:
        if style.startswith("Heading "):
            document.add_heading(text, level=int(style.split()[1]))
        else:
            document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()

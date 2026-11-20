"""Image Loader, the OCR node and OCR inside the Document Loader (fake engine; the live engine is opt-in)."""

from __future__ import annotations

import io
import os

import pytest
from agenticmind_node_sdk import NodeError
from agenticmind_node_sdk.testing import MemoryArtifacts, invoke_node
from agenticmind_nodes_data import ocr_engine as ocr_module
from agenticmind_nodes_data.nodes import document_loader, image_loader, ocr
from agenticmind_nodes_data.ocr_engine import OcrLine, ocr_bytes

PNG = b"\x89PNG\r\n\x1a\nreceipt"


@pytest.fixture
def engine():
    """Fake OCR: lines out of order and one low-confidence line, keyed by the image bytes."""
    calls = []

    def run(data: bytes):
        calls.append(data)
        return [
            OcrLine("Total: 12.50", 0.97, x=10, y=60),
            OcrLine("smudge", 0.2, x=10, y=90),
            OcrLine("Coffee", 0.99, x=10, y=20),
            OcrLine("x2", 0.95, x=200, y=24),
        ]

    ocr_module.set_engine(run)
    yield calls
    ocr_module.set_engine(None)


def scanned_pdf(pages: int = 2) -> bytes:
    """A PDF whose pages are images only (no text layer), like a scanner's output."""
    from PIL import Image

    images = [Image.new("RGB", (200, 100), "white") for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def test_reading_order_rows_and_confidence(engine):
    result = ocr_bytes(b"img")
    assert result.text == "Coffee x2\nTotal: 12.50"  # top to bottom, same row joined, low confidence dropped
    assert len(result.lines) == 3 and result.mean_confidence == pytest.approx(0.97)


async def test_image_loader_outputs_image_references():
    files = MemoryArtifacts({"a1": ("a.png", PNG), "a2": ("b.gif", b"GIF89a"), "t": ("t.txt", b"x")})
    r = await invoke_node(image_loader, config={"files": ["a1"]}, inputs={"files": ["a2"]}, artifacts=files)
    assert r.outputs["image"]["artifact_id"] == "a1"
    assert [i["media_type"] for i in r.outputs["images"]] == ["image/png", "image/gif"]
    with pytest.raises(NodeError) as exc:
        await invoke_node(image_loader, config={"files": ["t"]}, artifacts=files)
    assert exc.value.code == "NOT_AN_IMAGE"


async def test_ocr_node_reads_images_and_scanned_pdfs(engine):
    files = MemoryArtifacts({"img": ("receipt.png", PNG), "pdf": ("scan.pdf", scanned_pdf(2))})
    image = {"kind": "image", "artifact_id": "img", "filename": "receipt.png", "media_type": "image/png"}
    r = await invoke_node(ocr, inputs={"image": image}, config={"files": ["pdf"]}, artifacts=files)
    docs = r.outputs["documents"]
    assert [d["source"].get("page") for d in docs] == [None, 1, 2]
    assert all(d["content"] == "Coffee x2\nTotal: 12.50" for d in docs)
    assert docs[0]["metadata"]["ocr"] == "rapidocr" and docs[1]["source"]["filename"] == "scan.pdf"
    assert r.outputs["text"].count("Coffee") == 3 and len(engine) == 3


async def test_document_loader_ocrs_images_and_pages_without_text(engine):
    files = MemoryArtifacts({"img": ("receipt.png", PNG), "pdf": ("scan.pdf", scanned_pdf(2))})
    r = await invoke_node(document_loader, config={"files": ["img", "pdf"]}, artifacts=files)
    docs = r.outputs["documents"]
    assert len(docs) == 3 and docs[1]["source"]["page"] == 1
    assert docs[1]["metadata"]["ocr_lines"] == 3 and docs[1]["metadata"]["page_count"] == 2
    joined = await invoke_node(document_loader, config={"files": ["pdf"], "split_pdf_pages": False}, artifacts=files)
    [doc] = joined.outputs["documents"]
    assert doc["content"].count("Coffee") == 2 and doc["metadata"]["ocr_pages"] == 2
    with pytest.raises(NodeError) as exc:
        await invoke_node(document_loader, config={"files": ["img"], "ocr": "off"}, artifacts=files)
    assert exc.value.code == "OCR_OFF"
    with pytest.raises(NodeError) as exc:
        await invoke_node(document_loader, config={"files": ["pdf"], "ocr": "off"}, artifacts=files)
    assert exc.value.code == "NO_TEXT_EXTRACTED" and "OCR" in str(exc.value)


async def test_text_pdfs_do_not_run_ocr(engine):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    files = MemoryArtifacts({"md": ("n.md", b"# Title\ntext"), "blank": ("blank.pdf", buf.getvalue())})
    r = await invoke_node(document_loader, config={"files": ["md"]}, artifacts=files)
    assert r.outputs["documents"][0]["content"].startswith("# Title") and engine == []
    with pytest.raises(NodeError) as exc:  # a blank page has neither text nor images
        await invoke_node(document_loader, config={"files": ["blank"]}, artifacts=files)
    assert exc.value.code == "NO_TEXT_EXTRACTED" and engine == []


@pytest.mark.skipif(not os.environ.get("AGENTICMIND_TEST_LIVE_OCR"), reason="set AGENTICMIND_TEST_LIVE_OCR=1")
def test_live_rapidocr_reads_rendered_text():  # pragma: no cover - downloads models on first use
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 120), "white")
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except OSError:
        font = ImageFont.load_default(size=40)
    ImageDraw.Draw(img).text((20, 30), "Invoice 4821", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert "4821" in ocr_bytes(buf.getvalue()).text

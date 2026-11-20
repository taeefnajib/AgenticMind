"""Open-source OCR (RapidOCR: PaddleOCR models on ONNX Runtime, Apache-2.0).

Installed with ``agenticmind-nodes-data[ocr]`` (the API installs it). The engine is created once per
process and lazily, because the first recognition loads the detection/recognition models (RapidOCR fetches
them on first use; ``rapidocr download_models`` pre-fetches them for offline machines).

Verified against RapidOCR 3.x docs (Nov 2026): ``RapidOCR()(image)`` accepts a path, bytes, ndarray or PIL
image and returns ``txts`` (lines), ``boxes`` (N x 4 x 2) and ``scores``.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .parsers import ParseError

ENGINE_NAME = "rapidocr"


@dataclass(frozen=True)
class OcrLine:
    text: str
    score: float
    #: Top-left corner (x, y) of the line's box, used to keep reading order.
    x: float = 0.0
    y: float = 0.0


@dataclass(frozen=True)
class OcrResult:
    text: str
    lines: list[OcrLine]

    @property
    def mean_confidence(self) -> float | None:
        return round(sum(line.score for line in self.lines) / len(self.lines), 3) if self.lines else None


#: (image bytes) -> recognised lines. Swappable for tests and other engines.
OcrEngine = Callable[[bytes], list[OcrLine]]

_engine: OcrEngine | None = None
_lock = threading.Lock()


def _rapidocr_engine() -> OcrEngine:
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ParseError(
            "OCR_UNAVAILABLE",
            "OCR needs the open-source RapidOCR engine: pip install 'agenticmind-nodes-data[ocr]'",
        ) from exc
    import logging

    engine = RapidOCR()
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)  # it logs every model load at INFO

    def run(data: bytes) -> list[OcrLine]:
        result: Any = engine(data)
        texts = list(getattr(result, "txts", None) or [])
        scores = list(getattr(result, "scores", None) or [1.0] * len(texts))
        boxes = getattr(result, "boxes", None)
        lines = []
        for i, text in enumerate(texts):
            x = y = 0.0
            if boxes is not None and len(boxes) > i:
                x, y = float(boxes[i][0][0]), float(boxes[i][0][1])
            lines.append(OcrLine(text=str(text), score=float(scores[i]), x=x, y=y))
        return lines

    return run


def get_engine() -> OcrEngine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = _rapidocr_engine()
        return _engine


def set_engine(engine: OcrEngine | None) -> None:
    """Replace the engine (tests); ``None`` goes back to RapidOCR on next use."""
    global _engine
    with _lock:
        _engine = engine


def _reading_order(lines: list[OcrLine]) -> list[OcrLine]:
    """Top-to-bottom, then left-to-right; lines whose tops are within ~12 px count as one row."""
    rows: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda line: (line.y, line.x)):
        if rows and abs(rows[-1][0].y - line.y) <= 12:
            rows[-1].append(line)
        else:
            rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda line: line.x)]


def ocr_bytes(data: bytes, *, min_confidence: float = 0.5) -> OcrResult:
    """Recognise text in one image. Lines below ``min_confidence`` are dropped."""
    try:
        raw = get_engine()(data)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - engines raise many unrelated types for unreadable images
        raise ParseError("OCR_FAILED", f"OCR could not read this image: {exc}") from exc
    kept = _reading_order([line for line in raw if line.score >= min_confidence and line.text.strip()])
    rows: list[str] = []
    last_y: float | None = None
    for line in kept:
        if last_y is not None and abs(line.y - last_y) <= 12 and rows:
            rows[-1] += " " + line.text.strip()
        else:
            rows.append(line.text.strip())
        last_y = line.y
    return OcrResult(text="\n".join(rows), lines=kept)


def pdf_page_images(data: bytes, pages: set[int]) -> dict[int, list[bytes]]:
    """Embedded images of the given 1-based pages (a scanned PDF has one image per page)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out: dict[int, list[bytes]] = {}
    for number in sorted(pages):
        if not 1 <= number <= len(reader.pages):
            continue
        images = []
        try:
            for image in reader.pages[number - 1].images:
                images.append(image.data)
        except Exception:  # noqa: BLE001 - unsupported image filters: OCR what can be read
            pass
        out[number] = images
    return out

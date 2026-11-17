"""Chunking algorithms that work on character spans.

Every chunk is an exact slice ``text[start:end]`` of its parent, so offsets always point back to
the precise source text (needed for citations and retrieval evaluation).

Sizes are measured either in characters or in approximate tokens. The token measure is a
dependency-free approximation (words and punctuation marks), close to but not identical with any
specific model tokenizer; it is labelled as approximate in the UI.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)

DEFAULT_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")

Span = tuple[int, int]


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    section: str | None = None


class Measurer:
    def __init__(self, text: str, unit: str) -> None:
        self.text = text
        self.unit = unit
        if unit == "tokens":
            matches = list(_TOKEN_RE.finditer(text))
            self.starts = [m.start() for m in matches]
            self.ends = [m.end() for m in matches]

    def __call__(self, start: int, end: int) -> int:
        if self.unit == "characters":
            return end - start
        # tokens fully inside [start, end)
        lo = bisect.bisect_left(self.starts, start)
        hi = bisect.bisect_right(self.ends, end)
        return max(0, hi - lo)


def _strip(text: str, start: int, end: int) -> Span:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _finalize(text: str, spans: Sequence[Span]) -> list[Span]:
    out = []
    for s, e in spans:
        s, e = _strip(text, s, e)
        if e > s:
            out.append((s, e))
    return out


def fixed_spans(text: str, start: int, end: int, size: int, overlap: int, measure: Measurer) -> list[Span]:
    """Sliding window of ``size`` units with ``overlap`` units shared between neighbours."""
    step = max(1, size - overlap)
    spans: list[Span] = []
    if measure.unit == "characters":
        i = start
        while i < end:
            j = min(i + size, end)
            spans.append((i, j))
            if j >= end:
                break
            i += step
        return spans
    lo = bisect.bisect_left(measure.starts, start)
    hi = bisect.bisect_right(measure.ends, end)
    tokens = list(range(lo, hi))
    if not tokens:
        return [(start, end)] if text[start:end].strip() else []
    i = 0
    while i < len(tokens):
        j = min(i + size, len(tokens))
        spans.append((measure.starts[tokens[i]], measure.ends[tokens[j - 1]]))
        if j >= len(tokens):
            break
        i += step
    return spans


def _split_keep(text: str, start: int, end: int, sep: str) -> list[Span]:
    """Split [start, end) after each occurrence of ``sep`` (separator stays with the left piece)."""
    pieces: list[Span] = []
    cursor = start
    while True:
        idx = text.find(sep, cursor, end)
        if idx == -1:
            break
        cut = idx + len(sep)
        pieces.append((cursor, cut))
        cursor = cut
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def recursive_spans(
    text: str,
    start: int,
    end: int,
    size: int,
    overlap: int,
    measure: Measurer,
    separators: Sequence[str] = DEFAULT_SEPARATORS,
) -> list[Span]:
    """Split on the coarsest separator that works, recurse on oversized pieces, then merge greedily."""

    def split(s: int, e: int, seps: Sequence[str]) -> list[Span]:
        if measure(s, e) <= size:
            return [(s, e)]
        for idx, sep in enumerate(seps):
            if sep and text.find(sep, s, e) != -1:
                out: list[Span] = []
                for ps, pe in _split_keep(text, s, e, sep):
                    out.extend(split(ps, pe, seps[idx + 1 :]))
                return out
        return fixed_spans(text, s, e, size, 0, measure)

    pieces = split(start, end, separators)
    chunks: list[Span] = []
    current: list[Span] = []
    for piece in pieces:
        if current and measure(current[0][0], piece[1]) > size:
            chunks.append((current[0][0], current[-1][1]))
            # keep a tail of the previous chunk as overlap, as long as the next piece still fits
            while current and (
                measure(current[0][0], current[-1][1]) > overlap or measure(current[0][0], piece[1]) > size
            ):
                current.pop(0)
        current.append(piece)
    if current:
        chunks.append((current[0][0], current[-1][1]))
    return chunks


def markdown_sections(text: str) -> list[tuple[int, int, str | None]]:
    """Sections delimited by Markdown headings, each with its heading path (``A > B > C``)."""
    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        return [(0, len(text), None)]
    sections: list[tuple[int, int, str | None]] = []
    if headings[0].start() > 0:
        sections.append((0, headings[0].start(), None))
    stack: list[tuple[int, str]] = []
    for i, h in enumerate(headings):
        level, title = len(h.group(1)), h.group(2).strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections.append((h.start(), end, " > ".join(t for _, t in stack)))
    return sections


def chunk_text(
    text: str,
    *,
    strategy: str,
    unit: str,
    size: int,
    overlap: int,
    separators: Sequence[str] = DEFAULT_SEPARATORS,
) -> list[Chunk]:
    if size < 1:
        raise ValueError("size must be >= 1")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be >= 0 and smaller than size")
    measure = Measurer(text, unit)
    runner: Callable[[int, int], list[Span]]
    if strategy == "fixed":
        runner = lambda s, e: fixed_spans(text, s, e, size, overlap, measure)  # noqa: E731
    else:
        runner = lambda s, e: recursive_spans(text, s, e, size, overlap, measure, separators)  # noqa: E731

    if strategy == "markdown":
        chunks: list[Chunk] = []
        for s, e, section in markdown_sections(text):
            chunks.extend(Chunk(cs, ce, section) for cs, ce in _finalize(text, runner(s, e)))
        return chunks
    if strategy not in ("fixed", "recursive"):
        raise ValueError(f"Unknown strategy {strategy!r}")
    return [Chunk(s, e) for s, e in _finalize(text, runner(0, len(text)))]

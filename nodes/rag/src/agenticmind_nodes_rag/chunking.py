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


# ---- step 3 strategies (ADR-0027) ---------------------------------------------------------------
# Every strategy below still yields exact spans of the parent text, so offsets and citations stay valid.

_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+|\n\s*\n|\n(?=\s*(?:[-*+•]|\d+[.)]|#{1,6}\s))")


def sentence_spans(text: str, start: int, end: int) -> list[Span]:
    """Sentences (and paragraph / list-item breaks) as spans; abbreviations may split early."""
    spans: list[Span] = []
    cursor = start
    for m in _SENTENCE_END.finditer(text, start, end):
        if m.start() > cursor:
            spans.append((cursor, m.start()))
        cursor = m.end()
    if cursor < end:
        spans.append((cursor, end))
    return _finalize(text, spans)


def pack_spans(
    text: str, units: Sequence[Span], size: int, overlap: int, measure: Measurer, separators: Sequence[str]
) -> list[Span]:
    """Greedily pack whole units (sentences, blocks) into chunks of at most ``size``; trailing units of the
    previous chunk are repeated while they fit in ``overlap``. A unit larger than ``size`` is split recursively."""
    chunks: list[Span] = []
    current: list[Span] = []
    for unit in units:
        if measure(unit[0], unit[1]) > size:
            if current:
                chunks.append((current[0][0], current[-1][1]))
                current = []
            chunks.extend(recursive_spans(text, unit[0], unit[1], size, overlap, measure, separators))
            continue
        if current and measure(current[0][0], unit[1]) > size:
            chunks.append((current[0][0], current[-1][1]))
            tail: list[Span] = []
            for prev in reversed(current):
                if overlap and measure(prev[0], current[-1][1]) <= overlap:
                    tail.insert(0, prev)
                else:
                    break
            current = [u for u in tail if measure(u[0], unit[1]) <= size]
        current.append(unit)
    if current:
        chunks.append((current[0][0], current[-1][1]))
    return chunks


# -- semantic -------------------------------------------------------------------------------------
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_STOP = frozenset(
    "the a an and or but of to in on at for with by from is are was were be been it this that these those as "
    "not no do does did have has had will would can could should may might its their our your we you they he "
    "she i me my so than then there here what which who when where how all any each also into about over".split()
)


def sentence_windows(text: str, sentences: Sequence[Span], buffer: int = 1) -> list[str]:
    """Each sentence with ``buffer`` neighbours on both sides (smooths noisy single-sentence similarity)."""
    out = []
    for i in range(len(sentences)):
        lo, hi = max(0, i - buffer), min(len(sentences), i + buffer + 1)
        out.append(text[sentences[lo][0] : sentences[hi - 1][1]])
    return out


def _stem(word: str) -> str:
    """Crude, language-light stemming: "volcanoes", "volcano" and "volcanic" all become "volcan"."""
    if len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    return word[:6]


def lexical_vectors(texts: Sequence[str]) -> list[dict[str, float]]:
    """Dependency-free fallback for semantic chunking: TF-IDF weighted bags of content words."""
    import math
    from collections import Counter

    bags = [Counter(_stem(w) for w in _WORD.findall(t.lower()) if w not in _STOP) for t in texts]
    df: Counter[str] = Counter()
    for bag in bags:
        df.update(bag.keys())
    n = len(bags) or 1
    return [{w: c * (math.log((1 + n) / (1 + df[w])) + 1) for w, c in bag.items()} for bag in bags]


def cosine(a: Sequence[float] | dict[str, float], b: Sequence[float] | dict[str, float]) -> float:
    import math

    if isinstance(a, dict) and isinstance(b, dict):
        dot = sum(v * b.get(k, 0.0) for k, v in a.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b, strict=False))  # type: ignore[arg-type]
        na = math.sqrt(sum(x * x for x in a))  # type: ignore[union-attr]
        nb = math.sqrt(sum(y * y for y in b))  # type: ignore[union-attr]
    return dot / (na * nb) if na and nb else 0.0


def semantic_spans(
    text: str,
    sentences: Sequence[Span],
    vectors: Sequence[Sequence[float] | dict[str, float]],
    *,
    size: int,
    min_size: int,
    percentile: float,
    measure: Measurer,
    separators: Sequence[str],
) -> tuple[list[Span], list[int]]:
    """Break between sentences where neighbouring windows are least similar: distances at or above the
    ``percentile``-th percentile mark a topic shift. Groups stay within ``size``; groups below ``min_size``
    are merged into a neighbour. Returns the chunks and the sentence indices where breaks happened."""
    if len(sentences) <= 1:
        return ([(sentences[0][0], sentences[-1][1])] if sentences else []), []
    distances = [1 - cosine(vectors[i], vectors[i + 1]) for i in range(len(sentences) - 1)]
    ordered = sorted(distances)
    rank = percentile / 100 * (len(ordered) - 1)  # linear interpolation, as numpy.percentile
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    cut = ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)
    breaks = [i + 1 for i, d in enumerate(distances) if d > cut]
    groups: list[list[Span]] = []
    start = 0
    for b in [*breaks, len(sentences)]:
        if b > start:
            groups.append(list(sentences[start:b]))
            start = b
    merged: list[list[Span]] = []
    for group in groups:  # undersized groups absorb the next one; the last one joins its predecessor
        if merged and measure(merged[-1][0][0], merged[-1][-1][1]) < min_size:
            merged[-1].extend(group)
        else:
            merged.append(group)
    if len(merged) > 1 and measure(merged[-1][0][0], merged[-1][-1][1]) < min_size:
        merged[-2].extend(merged.pop())
    chunks: list[Span] = []
    for group in merged:
        if measure(group[0][0], group[-1][1]) <= size:
            chunks.append((group[0][0], group[-1][1]))
        else:  # a long topic: pack its sentences
            chunks.extend(pack_spans(text, group, size, 0, measure, separators))
    return chunks, breaks


# -- layout (structure-aware) ---------------------------------------------------------------------
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_TABLE_ROW = re.compile(r"^\s*\||^[^|\n]*\|[^|\n]*\|")


@dataclass(frozen=True)
class Block:
    start: int
    end: int
    kind: str  # heading | paragraph | list | table | code


def _line_kind(line: str) -> str:
    if not line.strip():
        return "blank"
    if _HEADING_RE.match(line):
        return "heading"
    if _FENCE.match(line):
        return "fence"
    if _TABLE_ROW.match(line):
        return "table"
    if _LIST_ITEM.match(line):
        return "list"
    return "paragraph"


def layout_blocks(text: str, start: int = 0, end: int | None = None) -> list[Block]:
    """Structural blocks of Markdown-ish text (what the loaders produce): headings, paragraphs, whole lists,
    whole tables and fenced code. Blank lines separate paragraphs; lists and tables run over consecutive lines."""
    end = len(text) if end is None else end
    lines: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        nl = text.find("\n", pos, end)
        stop = end if nl == -1 else nl
        lines.append((pos, stop))
        pos = stop + 1
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        s, e = lines[i]
        k = _line_kind(text[s:e])
        if k == "blank":
            i += 1
            continue
        if k == "heading":
            blocks.append(Block(s, e, "heading"))
            i += 1
            continue
        if k == "fence":
            j = i + 1
            while j < len(lines) and not _FENCE.match(text[lines[j][0] : lines[j][1]]):
                j += 1
            last = min(j, len(lines) - 1)
            blocks.append(Block(s, lines[last][1], "code"))
            i = last + 1
            continue
        j = i + 1
        while j < len(lines):
            line = text[lines[j][0] : lines[j][1]]
            nk = _line_kind(line)
            continues_item = k == "list" and nk == "paragraph" and line[:1].isspace()
            if nk == k or continues_item:
                j += 1
            else:
                break
        blocks.append(Block(s, lines[j - 1][1], k))
        i = j
    return blocks


def _split_block(text: str, block: Block, size: int, measure: Measurer, separators: Sequence[str]) -> list[Span]:
    """An oversized block: tables/lists/code by whole lines (rows, items), paragraphs recursively."""
    if block.kind in ("table", "list", "code"):
        rows = _finalize(text, _split_keep(text, block.start, block.end, "\n"))
        return pack_spans(text, rows, size, 0, measure, separators)
    return recursive_spans(text, block.start, block.end, size, 0, measure, separators)


def layout_chunks(text: str, size: int, measure: Measurer, separators: Sequence[str]) -> list[Chunk]:
    """Chunks that follow the document's structure: a new chunk at every heading, headings kept with the
    content below them, and tables, lists and code blocks never cut unless one alone exceeds ``size``."""
    chunks: list[Chunk] = []
    for sec_start, sec_end, section in markdown_sections(text):
        current: list[Block] = []
        for block in layout_blocks(text, sec_start, sec_end):
            if measure(block.start, block.end) > size:
                if current and not all(b.kind == "heading" for b in current):
                    chunks.append(Chunk(current[0].start, current[-1].end, section))
                    current = []
                pieces = _split_block(text, block, size, measure, separators)
                if current:  # a lone heading joins the first piece of what it introduces
                    pieces[0] = (current[0].start, pieces[0][1])
                    current = []
                chunks.extend(Chunk(s, e, section) for s, e in pieces)
                continue
            only_headings = all(b.kind == "heading" for b in current)
            if current and not only_headings and measure(current[0].start, block.end) > size:
                chunks.append(Chunk(current[0].start, current[-1].end, section))
                current = []
            current.append(block)
        if current:
            chunks.append(Chunk(current[0].start, current[-1].end, section))
    return chunks


def parent_child_chunks(
    text: str, *, parent_size: int, size: int, overlap: int, measure: Measurer, separators: Sequence[str]
) -> list[tuple[Span, list[Span]]]:
    """Large parent sections (for the model's context), each split into small children (for retrieval)."""
    out = []
    for ps, pe in _finalize(text, recursive_spans(text, 0, len(text), parent_size, 0, measure, separators)):
        children = _finalize(text, recursive_spans(text, ps, pe, size, overlap, measure, separators))
        out.append(((ps, pe), children))
    return out

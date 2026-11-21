"""Deterministic guardrail checks (no model calls): PII detection and redaction, prompt-injection
heuristics, banned words, regex rules, length and JSON / JSON Schema validation.

Every check returns plain data so nodes can combine them; the LLM-judged checks live in ``guardrails.py``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Action = Literal["block", "flag", "redact"]


@dataclass
class Violation:
    check: str
    action: Action
    detail: str
    matches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["matches"]:
            data.pop("matches")
        return data


# ---- PII ------------------------------------------------------------------------------------------
PII_TYPES = ("email", "phone", "credit_card", "ssn", "iban", "ip_address", "secret")
DEFAULT_PII = ("email", "phone", "credit_card", "ssn", "iban", "secret")

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{9,15}|(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{2,4}(?:[\s.-]?\d{2,5}){1,2})(?![\w])"
)
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_SSN = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
# Common credential formats (provider key prefixes, private keys, JWTs). Patterns, not real values.
_SECRET = re.compile(
    "|".join(
        [
            r"\bsk-[A-Za-z0-9_-]{20,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bgh[pousr]_[A-Za-z0-9]{30,}",
            r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
            r"\bhf_[A-Za-z0-9]{30,}",
            r"\bAIza[0-9A-Za-z_-]{35}",
            r"\bnvapi\-[A-Za-z0-9_-]{20,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        ]
    )
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def luhn_ok(number: str) -> bool:
    digits = [int(d) for d in _digits(number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    s = value.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


_DETECTORS: list[tuple[str, re.Pattern[str], Any]] = [
    ("secret", _SECRET, None),
    ("email", _EMAIL, None),
    ("iban", _IBAN, iban_ok),
    ("credit_card", _CARD, luhn_ok),
    ("ssn", _SSN, None),
    ("ip_address", _IPV4, None),
    ("phone", _PHONE, lambda m: 9 <= len(_digits(m)) <= 15),
]


@dataclass
class PiiMatch:
    type: str
    start: int
    end: int
    text: str


def find_pii(text: str, types: tuple[str, ...] | list[str] = DEFAULT_PII) -> list[PiiMatch]:
    """Non-overlapping PII matches; earlier detectors win (a card number is not also a phone number)."""
    wanted = set(types)
    found: list[PiiMatch] = []
    taken: list[tuple[int, int]] = []
    for name, pattern, valid in _DETECTORS:
        if name not in wanted:
            continue
        for m in pattern.finditer(text):
            s, e = m.span()
            if valid is not None and not valid(m.group()):
                continue
            if any(s < te and ts < e for ts, te in taken):
                continue
            taken.append((s, e))
            found.append(PiiMatch(name, s, e, m.group()))
    return sorted(found, key=lambda x: x.start)


def redact(text: str, matches: list[PiiMatch], style: Literal["label", "mask", "hash"] = "label") -> str:
    out, pos = [], 0
    for m in matches:
        out.append(text[pos : m.start])
        if style == "mask":
            keep = m.text[-4:] if m.type in ("credit_card", "iban", "phone") else ""
            out.append("*" * max(len(m.text) - len(keep), 3) + keep)
        elif style == "hash":
            out.append(f"[{m.type.upper()}:{hashlib.sha256(m.text.encode()).hexdigest()[:8]}]")
        else:
            out.append(f"[{m.type.upper()}]")
        pos = m.end
    out.append(text[pos:])
    return "".join(out)


def mask_preview(m: PiiMatch) -> str:
    """A safe preview for reports: never the whole value."""
    t = m.text
    return f"{m.type}: {t[:2]}…{t[-2:]}" if len(t) > 6 else f"{m.type}: …"


# ---- prompt injection -----------------------------------------------------------------------------
_INJECTION_PATTERNS: list[tuple[str, float, str]] = [
    (
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|any|your|the)\b.{0,30}"
        r"\b(instructions?|prompts?|rules?|directions?|guidelines?|messages?)\b",
        0.6,
        "asks to ignore instructions",
    ),
    (
        r"\b(reveal|show|print|repeat|output|leak|tell me)\b.{0,40}\b(system|hidden|initial|original|secret)\b.{0,20}"
        r"\b(prompt|instructions?|message|rules)\b",
        0.55,
        "asks for the system prompt",
    ),
    (r"\byou are (now|no longer)\b|\bfrom now on,? you\b|\bnew (role|persona|instructions?)\b", 0.35, "role override"),
    (r"\b(dan|do anything now|developer mode|jailbreak|jailbroken|god mode)\b", 0.5, "known jailbreak phrasing"),
    (
        r"\b(pretend|act as if|imagine)\b.{0,30}\b(no|without)\b.{0,20}"
        r"\b(rules|restrictions|filters|limits|guidelines)\b",
        0.5,
        "asks to drop restrictions",
    ),
    (
        r"<\|im_start\|>|<\|system\|>|\[/?inst\]|<<sys>>",
        0.55,
        "fake chat-template tokens",
    ),
    (
        r"^\s*#{2,}\s*(system|instruction)|^\s*system\s*(prompt)?\s*:",
        0.3,
        "fake system-message header",
    ),
    (r"\b(execute|run)\b.{0,20}\b(this|the following)\b.{0,20}\b(code|command|script)\b", 0.25, "asks to run code"),
    (
        r"\b(send|post|forward|upload|exfiltrate)\b.{0,40}\b(to|at)\b.{0,10}(https?://|\S+@\S+)",
        0.4,
        "asks to send data to an address",
    ),
]
_COMPILED = [(re.compile(p, re.I | re.M | re.S), w, why) for p, w, why in _INJECTION_PATTERNS]
_ZERO_WIDTH = re.compile("[​-‏⁠-⁤﻿]")
_TAG_CHARS = re.compile("[\U000e0000-\U000e007f]")
_BASE64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _ZERO_WIDTH.sub("", _TAG_CHARS.sub("", text))
    return re.sub(r"[ \t]+", " ", text)


def _decoded_blobs(text: str) -> list[str]:
    out = []
    for blob in _BASE64.findall(text)[:10]:
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if sum(c.isprintable() or c.isspace() for c in decoded) >= 0.9 * len(decoded):
            out.append(decoded)
    return out


def injection_score(text: str) -> tuple[float, list[str]]:
    """Heuristic prompt-injection score in [0, 1] and the reasons. Catches common phrasings, fake chat
    markers, invisible Unicode tag / zero-width characters and base64-wrapped instructions; it is a
    first filter, not a guarantee (pair it with the LLM judge for important inputs)."""
    reasons: list[str] = []
    score = 0.0
    if _TAG_CHARS.search(text):
        score += 0.6
        reasons.append("hidden Unicode tag characters")
    if len(_ZERO_WIDTH.findall(text)) >= 3:
        score += 0.2
        reasons.append("zero-width characters")
    clean = normalise(text)
    for candidate, prefix in [(clean, ""), *((normalise(d), "base64: ") for d in _decoded_blobs(clean))]:
        for pattern, weight, why in _COMPILED:
            if pattern.search(candidate):
                score += weight + (0.2 if prefix else 0.0)
                reasons.append(prefix + why)
    return min(1.0, round(score, 3)), list(dict.fromkeys(reasons))


# ---- words, regex, length, JSON -------------------------------------------------------------------
def banned_hits(text: str, words: list[str]) -> list[str]:
    hits = []
    low = normalise(text).lower()
    for w in words:
        w = w.strip().lower()
        if w and re.search(rf"(?<!\w){re.escape(w)}(?!\w)", low):
            hits.append(w)
    return hits


def regex_hits(text: str, patterns: list[str]) -> list[str]:
    hits = []
    for p in patterns:
        try:
            if re.search(p, text, re.I | re.M):
                hits.append(p)
        except re.error as exc:
            raise ValueError(f"invalid regex {p!r}: {exc}") from exc
    return hits


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply: the whole text, a fenced block, or the outermost {...} / [...]."""
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    candidates = [s] + ([fence.group(1).strip()] if fence else [])
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = s.find(open_c), s.rfind(close_c)
        if 0 <= a < b:
            candidates.append(s[a : b + 1])
    last: Exception | None = None
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError as exc:
            last = exc
    raise ValueError(f"not valid JSON ({last})")


def schema_errors(value: Any, schema: dict[str, Any], limit: int = 5) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.absolute_path))
    out = []
    for e in errors[:limit]:
        where = "/".join(str(p) for p in e.absolute_path) or "(root)"
        out.append(f"{where}: {e.message}")
    return out

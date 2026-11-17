"""Safe summaries of node inputs/outputs for traces.

Traces never contain secret values, and large payloads are truncated by default.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "***"
_SENSITIVE_KEY = re.compile(r"(api[_-]?key|secret|password|passwd|token|authorization|credential)", re.I)


def summarize(
    value: Any,
    *,
    secrets: Iterable[str] = (),
    max_string: int = 500,
    max_items: int = 20,
    max_depth: int = 6,
) -> Any:
    secret_values = [s for s in secrets if s]

    def scrub(text: str) -> str:
        for s in secret_values:
            text = text.replace(s, REDACTED)
        return text

    def walk(v: Any, depth: int) -> Any:
        if depth > max_depth:
            return "…"
        if isinstance(v, str):
            v = scrub(v)
            return v if len(v) <= max_string else f"{v[:max_string]}…(+{len(v) - max_string} chars)"
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, dict):
            out: dict[str, Any] = {}
            for i, (k, item) in enumerate(v.items()):
                if i >= max_items:
                    out["…"] = f"+{len(v) - max_items} keys"
                    break
                out[str(k)] = REDACTED if _SENSITIVE_KEY.search(str(k)) else walk(item, depth + 1)
            return out
        if isinstance(v, (list, tuple)):
            items = [walk(x, depth + 1) for x in list(v)[:max_items]]
            if len(v) > max_items:
                items.append(f"…(+{len(v) - max_items} items)")
            return items
        return scrub(f"<{type(v).__name__}>")

    return walk(value, 0)

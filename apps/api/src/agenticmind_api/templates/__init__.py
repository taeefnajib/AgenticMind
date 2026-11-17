"""Bundled starter templates (plain IR documents)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent


@cache
def _all() -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(_DIR.glob("*.json"))}


def list_templates() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "title": doc["project"]["name"],
            "description": doc["project"].get("description", ""),
            "node_count": len(doc.get("nodes", [])),
        }
        for name, doc in _all().items()
    ]


def load_template(name: str) -> dict[str, Any]:
    templates = _all()
    if name not in templates:
        raise KeyError(name)
    return json.loads(json.dumps(templates[name]))

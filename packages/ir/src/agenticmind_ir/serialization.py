"""Deterministic serialization so that IR documents diff meaningfully."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import ProjectDocument

_OPTIONAL_NODE_KEYS = ("execution", "ui")


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def to_canonical_dict(doc: ProjectDocument) -> dict[str, Any]:
    """Dump to plain JSON data with stable ordering.

    Nodes and edges are sorted by ID. Optional structural fields that are unset are omitted;
    user-authored config values (including explicit ``null``) are preserved untouched.
    """
    data = doc.model_dump(mode="json", by_alias=True)
    nodes = []
    for node in data["nodes"]:
        for key in _OPTIONAL_NODE_KEYS:
            if node.get(key) is None:
                node.pop(key, None)
            else:
                node[key] = _drop_none(node[key])
        nodes.append(node)
    data["nodes"] = sorted(nodes, key=lambda n: n["id"])
    data["edges"] = sorted(data["edges"], key=lambda e: e["id"])
    return data


def to_canonical_json(doc: ProjectDocument) -> str:
    return json.dumps(to_canonical_dict(doc), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def content_hash(doc: ProjectDocument) -> str:
    compact = json.dumps(to_canonical_dict(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(compact.encode("utf-8")).hexdigest()

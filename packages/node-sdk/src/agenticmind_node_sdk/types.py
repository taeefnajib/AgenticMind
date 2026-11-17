"""Shared port type system.

Compatibility is deliberately strict: a value flows along a data edge only when the target port
accepts the source type. The only implicit acceptance is *subtype* acceptance that never changes
the value (``String``/``Number``/``Boolean`` are valid ``JSON``; anything is valid for ``Any``).
Everything else needs an explicit conversion node.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any


class PortType(str, Enum):
    ANY = "Any"
    STRING = "String"
    NUMBER = "Number"
    BOOLEAN = "Boolean"
    JSON = "JSON"
    DOCUMENT = "Document"
    DOCUMENT_SET = "DocumentSet"
    EMBEDDING_VECTOR = "EmbeddingVector"
    VECTOR_STORE_HANDLE = "VectorStoreHandle"
    CHAT_MESSAGE = "ChatMessage"
    TOOL = "Tool"
    AGENT_STATE = "AgentState"
    EVALUATION_DATASET = "EvaluationDataset"
    HTTP_REQUEST = "HTTPRequest"
    HTTP_RESPONSE = "HTTPResponse"


_JSON_SUBTYPES = {PortType.STRING, PortType.NUMBER, PortType.BOOLEAN, PortType.JSON}


def is_compatible(source: PortType, target: PortType) -> bool:
    """Whether an output of type ``source`` may feed an input of type ``target``."""
    if target == PortType.ANY or source == target:
        return True
    if target == PortType.JSON and source in _JSON_SUBTYPES:
        return True
    return False


def is_json_value(value: Any, _depth: int = 0) -> bool:
    if _depth > 64:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_json_value(v, _depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_json_value(v, _depth + 1) for k, v in value.items())
    return False


def value_matches_type(value: Any, port_type: PortType) -> bool:
    """Runtime check that a produced value honours its declared port type."""
    match port_type:
        case PortType.ANY:
            return True
        case PortType.STRING:
            return isinstance(value, str)
        case PortType.NUMBER:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        case PortType.BOOLEAN:
            return isinstance(value, bool)
        case PortType.JSON:
            return is_json_value(value)
        case PortType.CHAT_MESSAGE:
            return isinstance(value, dict) and "role" in value and "content" in value
        case PortType.EMBEDDING_VECTOR:
            return isinstance(value, list) and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
            )
        case _:
            # Domain types (Document, DocumentSet, ...) get schema-level checks when their
            # node families land (see docs/roadmap.md). Until then they are opaque.
            return True

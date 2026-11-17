"""Parse raw input into a validated ``ProjectDocument``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from .migrations import UnsupportedIRVersionError, migrate
from .models import ProjectDocument


class IRLoadError(ValueError):
    """Raised when a document cannot be parsed into the canonical IR.

    ``issues`` is a list of ``{"code", "message", "path"}`` dicts suitable for diagnostics.
    """

    def __init__(self, message: str, issues: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.issues = issues


def load_project(source: str | bytes | Mapping[str, Any]) -> ProjectDocument:
    if isinstance(source, (str, bytes)):
        try:
            raw = json.loads(source)
        except json.JSONDecodeError as exc:
            raise IRLoadError(
                "Invalid JSON", [{"code": "IR_INVALID_JSON", "message": str(exc), "path": ""}]
            ) from exc
    else:
        raw = source
    if not isinstance(raw, Mapping):
        raise IRLoadError(
            "IR document must be a JSON object",
            [{"code": "IR_SCHEMA_VIOLATION", "message": "Document must be a JSON object", "path": ""}],
        )
    try:
        upgraded = migrate(raw)
    except UnsupportedIRVersionError as exc:
        raise IRLoadError(
            str(exc), [{"code": "IR_UNSUPPORTED_VERSION", "message": str(exc), "path": "ir_version"}]
        ) from exc
    try:
        return ProjectDocument.model_validate(upgraded)
    except ValidationError as exc:
        issues = [
            {
                "code": "IR_SCHEMA_VIOLATION",
                "message": err["msg"],
                "path": ".".join(str(p) for p in err["loc"]),
            }
            for err in exc.errors()
        ]
        raise IRLoadError("IR document failed schema validation", issues) from exc

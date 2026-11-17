"""Published JSON Schema for the IR."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import IR_VERSION, ProjectDocument

SCHEMA_ID = f"https://agenticmind.dev/schemas/ir-v{IR_VERSION}.schema.json"


def ir_json_schema() -> dict[str, Any]:
    schema = ProjectDocument.model_json_schema(by_alias=True, mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": f"AgenticMind IR v{IR_VERSION}",
        **{k: v for k, v in schema.items() if k != "title"},
    }


def published_schema_path() -> Path:
    """Location of the committed schema file inside the source tree."""
    return Path(__file__).resolve().parents[2] / "schema" / f"ir-v{IR_VERSION}.schema.json"


def render_schema() -> str:
    return json.dumps(ir_json_schema(), indent=2, sort_keys=True) + "\n"

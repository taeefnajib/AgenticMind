"""Base classes and helpers for typed node configuration."""

from __future__ import annotations

from typing import Any

from agenticmind_ir import SECRET_REF_KEY
from agenticmind_ir.secrets import SECRET_NAME_PATTERN
from pydantic import BaseModel, ConfigDict, Field


class NodeConfig(BaseModel):
    """Base for every node's configuration model. Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmptyConfig(NodeConfig):
    pass


class SecretRef(BaseModel):
    """A reference to a secret held outside the IR: ``{"$secret": "NAME"}``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str = Field(alias=SECRET_REF_KEY, pattern=SECRET_NAME_PATTERN)


def ui(
    *,
    widget: str | None = None,
    advanced: bool = False,
    placeholder: str | None = None,
    secret: bool = False,
    show_when: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Build ``json_schema_extra`` UI hints consumed by the visual inspector.

    ``advanced`` fields are hidden in no-code mode. ``show_when={"engine": ["mysql", "sqlserver"]}`` shows a
    field only while another field has one of those values. Hints never change semantics.
    """
    hints: dict[str, Any] = {}
    if widget:
        hints["widget"] = widget
    if advanced:
        hints["advanced"] = True
    if placeholder:
        hints["placeholder"] = placeholder
    if show_when:
        hints["show_when"] = show_when
    extra: dict[str, Any] = {"x-ui": hints}
    if secret:
        extra["x-secret"] = True
    return extra


def secret_field(
    description: str = "", *, required: bool = False, show_when: dict[str, list[str]] | None = None
) -> Any:
    """Declare a config field holding a secret reference."""
    extra = ui(secret=True, show_when=show_when)
    if required:
        return Field(description=description, json_schema_extra=extra)
    return Field(default=None, description=description, json_schema_extra=extra)


def secret_fields(schema: dict[str, Any]) -> set[str]:
    return {name for name, prop in schema.get("properties", {}).items() if prop.get("x-secret")}

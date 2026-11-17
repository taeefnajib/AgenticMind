"""Machine-readable node manifest (the node contract)."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from agenticmind_ir import DEFAULT_BRANCH, IR_VERSION
from agenticmind_ir.models import NODE_TYPE_PATTERN, PORT_NAME_PATTERN, SEMVER_PATTERN
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .types import PortType

_PORT_RE = re.compile(PORT_NAME_PATTERN)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Cardinality(str, Enum):
    SINGLE = "single"
    MULTIPLE = "multiple"


class Mode(str, Enum):
    NO_CODE = "no_code"
    LOW_CODE = "low_code"
    DEVELOPER = "developer"


class InputPort(_Model):
    name: str = Field(pattern=PORT_NAME_PATTERN)
    type: PortType
    required: bool = True
    cardinality: Cardinality = Cardinality.SINGLE
    description: str = ""
    type_from_config: str | None = Field(
        default=None, description="Config field whose value (a PortType name) overrides `type`."
    )


class OutputPort(_Model):
    name: str = Field(pattern=PORT_NAME_PATTERN)
    type: PortType
    description: str = ""
    type_from_config: str | None = None


class Permissions(_Model):
    network: bool = False
    filesystem: bool = False
    database: bool = False
    subprocess: bool = False
    credentials: bool = Field(default=False, description="May resolve secrets referenced in its own config.")
    artifacts: bool = Field(default=False, description="May read files uploaded to the run's project.")


class ExecutionPolicy(_Model):
    timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    max_attempts: int = Field(default=1, ge=1, le=10)
    idempotent: bool = Field(
        default=True, description="Non-idempotent nodes are never retried automatically after a timeout."
    )


class Compatibility(_Model):
    ir_versions: list[str] = Field(default_factory=lambda: [IR_VERSION])
    adapters: list[str] = Field(default_factory=list)


class NodeManifest(_Model):
    type: str = Field(pattern=NODE_TYPE_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    display_name: str
    category: str
    package: str
    description: str = ""
    inputs: list[InputPort] = Field(default_factory=list)
    outputs: list[OutputPort] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=lambda: [DEFAULT_BRANCH])
    config_schema: dict[str, Any] = Field(default_factory=dict)
    permissions: Permissions = Field(default_factory=Permissions)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)
    compatibility: Compatibility = Field(default_factory=Compatibility)
    modes: list[Mode] = Field(default_factory=lambda: list(Mode))
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> NodeManifest:
        for kind, ports in (("input", self.inputs), ("output", self.outputs)):
            names = [p.name for p in ports]
            if len(names) != len(set(names)):
                raise ValueError(f"Duplicate {kind} port names in {self.type}")
        if not self.branches:
            raise ValueError("A node must declare at least one branch")
        for b in self.branches:
            if not _PORT_RE.match(b):
                raise ValueError(f"Invalid branch name {b!r}")
        props = self.config_schema.get("properties", {})
        for port in [*self.inputs, *self.outputs]:
            if port.type_from_config and port.type_from_config not in props:
                raise ValueError(f"Port {port.name} type_from_config refers to unknown field")
        return self

    def input(self, name: str) -> InputPort | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str) -> OutputPort | None:
        return next((p for p in self.outputs if p.name == name), None)


def resolve_port_type(port: InputPort | OutputPort, config: dict[str, Any]) -> PortType:
    """Effective port type given a node's (raw) config."""
    if port.type_from_config:
        value = config.get(port.type_from_config)
        if value is None:
            # fall back to the field default declared on the port's static type
            return port.type
        try:
            return PortType(value)
        except ValueError:
            return port.type
    return port.type

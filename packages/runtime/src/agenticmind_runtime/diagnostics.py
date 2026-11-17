"""Structured diagnostics with stable error codes.

Codes are part of the public contract (UI, CLI and API clients match on them). Never rename or
reuse a code; add new ones instead. The table is documented in docs/architecture/runtime.md.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Code(str, Enum):
    # 1. parse
    IR_INVALID_JSON = "IR_INVALID_JSON"
    IR_SCHEMA_VIOLATION = "IR_SCHEMA_VIOLATION"
    IR_UNSUPPORTED_VERSION = "IR_UNSUPPORTED_VERSION"
    # 2. registry resolution
    NODE_UNKNOWN_TYPE = "NODE_UNKNOWN_TYPE"
    NODE_UNKNOWN_VERSION = "NODE_UNKNOWN_VERSION"
    NODE_INCOMPATIBLE_IR = "NODE_INCOMPATIBLE_IR"
    NODE_ISOLATION_UNAVAILABLE = "NODE_ISOLATION_UNAVAILABLE"
    # 3. config
    CONFIG_INVALID = "CONFIG_INVALID"
    # 4. ports / edges
    EDGE_UNKNOWN_NODE = "EDGE_UNKNOWN_NODE"
    EDGE_UNKNOWN_PORT = "EDGE_UNKNOWN_PORT"
    EDGE_TYPE_MISMATCH = "EDGE_TYPE_MISMATCH"
    EDGE_INVALID_CONTROL_TARGET = "EDGE_INVALID_CONTROL_TARGET"
    EDGE_SELF_LOOP = "EDGE_SELF_LOOP"
    EDGE_DUPLICATE = "EDGE_DUPLICATE"
    PORT_MISSING_REQUIRED_INPUT = "PORT_MISSING_REQUIRED_INPUT"
    PORT_CARDINALITY_EXCEEDED = "PORT_CARDINALITY_EXCEEDED"
    # 5. graph structure
    GRAPH_EMPTY = "GRAPH_EMPTY"
    GRAPH_DUPLICATE_NODE_ID = "GRAPH_DUPLICATE_NODE_ID"
    GRAPH_DUPLICATE_EDGE_ID = "GRAPH_DUPLICATE_EDGE_ID"
    GRAPH_CYCLE = "GRAPH_CYCLE"
    GRAPH_ISOLATED_NODE = "GRAPH_ISOLATED_NODE"
    # 6. secrets / capabilities
    SECRET_LITERAL_VALUE = "SECRET_LITERAL_VALUE"
    SECRET_NOT_PERMITTED = "SECRET_NOT_PERMITTED"
    SECRET_NOT_CONFIGURED = "SECRET_NOT_CONFIGURED"
    # run planning
    RUN_TARGET_UNKNOWN = "RUN_TARGET_UNKNOWN"


class Diagnostic(BaseModel):
    code: Code
    severity: Severity = Severity.ERROR
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    port: str | None = None
    path: str | None = Field(default=None, description="Field path, e.g. config.chunk_size")
    related_node_ids: list[str] = Field(default_factory=list)
    hint: str | None = None


class ValidationReport(BaseModel):
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(d.severity == Severity.ERROR for d in self.diagnostics)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.severity == Severity.ERROR]

    def to_public(self) -> dict:
        return {"valid": self.valid, "diagnostics": [d.model_dump(mode="json") for d in self.diagnostics]}

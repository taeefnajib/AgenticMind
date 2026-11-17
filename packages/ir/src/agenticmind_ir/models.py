"""Canonical IR v1 models.

The IR is the durable source of truth for a project's architecture. It is deliberately
framework-independent: it knows about nodes, typed ports and edges, but nothing about
specific node implementations (node configuration is validated later against the node's
own schema, resolved from the registry).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

IR_VERSION = "1.0"

NODE_ID_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$"
NODE_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$"
SEMVER_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?$"
PORT_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
PROJECT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: Name of the implicit control-flow input port every node has.
CONTROL_TRIGGER_PORT = "trigger"
#: Branch activated by non-branching nodes when they succeed.
DEFAULT_BRANCH = "done"


class _IRModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ExecutionProfile(str, Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    ENTERPRISE = "enterprise"


class ProjectInfo(_IRModel):
    id: str = Field(pattern=PROJECT_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class RuntimeSpec(_IRModel):
    engine: Literal["agenticmind"] = "agenticmind"
    profile: ExecutionProfile = ExecutionProfile.DEVELOPMENT


class Position(_IRModel):
    x: float
    y: float


class NodeUI(_IRModel):
    """Presentation-only metadata. Never affects execution semantics."""

    position: Position | None = None
    label: str | None = Field(default=None, max_length=200)


class ExecutionOverrides(_IRModel):
    """Per-node overrides of the node manifest's execution policy."""

    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_attempts: int | None = Field(default=None, ge=1, le=10)


class Node(_IRModel):
    id: str = Field(pattern=NODE_ID_PATTERN)
    type: str = Field(pattern=NODE_TYPE_PATTERN, description="Stable node type ID, e.g. agenticmind.core.output")
    version: str = Field(pattern=SEMVER_PATTERN, description="Semantic version of the node package contract")
    config: dict[str, JsonValue] = Field(
        default_factory=dict,
        description='Node configuration. Secrets MUST be references: {"$secret": "NAME"}.',
    )
    execution: ExecutionOverrides | None = None
    ui: NodeUI | None = None


class EdgeKind(str, Enum):
    DATA = "data"
    CONTROL = "control"


class PortRef(_IRModel):
    node: str = Field(pattern=NODE_ID_PATTERN)
    port: str = Field(pattern=PORT_NAME_PATTERN)


class Edge(_IRModel):
    """A connection between two nodes.

    * ``data`` edges carry a typed value from an output port to an input port.
    * ``control`` edges go from a source *branch* (``done`` for ordinary nodes, or a declared
      branch such as ``true``/``false``) to the target's ``trigger`` port, and determine
      ordering and routing only.
    """

    id: str = Field(pattern=NODE_ID_PATTERN)
    kind: EdgeKind
    source: PortRef
    target: PortRef


class ProjectDocument(_IRModel):
    ir_version: Literal["1.0"] = IR_VERSION
    project: ProjectInfo
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)

"""AgenticMind canonical IR."""

from .loader import IRLoadError, load_project
from .migrations import UnsupportedIRVersionError, migrate, register_migration
from .models import (
    CONTROL_TRIGGER_PORT,
    DEFAULT_BRANCH,
    IR_VERSION,
    Edge,
    EdgeKind,
    ExecutionOverrides,
    ExecutionProfile,
    Node,
    NodeUI,
    PortRef,
    Position,
    ProjectDocument,
    ProjectInfo,
    RuntimeSpec,
)
from .schema import ir_json_schema
from .secrets import SECRET_REF_KEY, is_secret_ref, iter_secret_refs, secret_ref
from .serialization import content_hash, to_canonical_dict, to_canonical_json

__all__ = [
    "CONTROL_TRIGGER_PORT",
    "DEFAULT_BRANCH",
    "IR_VERSION",
    "SECRET_REF_KEY",
    "Edge",
    "EdgeKind",
    "ExecutionOverrides",
    "ExecutionProfile",
    "IRLoadError",
    "Node",
    "NodeUI",
    "Position",
    "PortRef",
    "ProjectDocument",
    "ProjectInfo",
    "RuntimeSpec",
    "UnsupportedIRVersionError",
    "content_hash",
    "ir_json_schema",
    "is_secret_ref",
    "iter_secret_refs",
    "load_project",
    "migrate",
    "register_migration",
    "secret_ref",
    "to_canonical_dict",
    "to_canonical_json",
]

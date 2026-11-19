"""What a compiled project needs from wherever it runs (ADR-0020).

The compiler declares requirements (secrets, network, local models, services); this module turns them, plus
the graph, into facts that targets can be checked against. Nothing here knows about any cloud.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from agenticmind_compiler import CompileResult
from agenticmind_ir import ProjectDocument
from agenticmind_registry import NodeRegistry

#: Nodes that write indexes under DATA_DIR at runtime (lost if the filesystem is not persistent).
_DISK_WRITERS = {"agenticmind.rag.index_documents"}
#: Nodes that actually read/write conversation state or memories. (Agents *declare* the memory permission so
#: they can use memory when wired to it; that alone does not make a graph stateful.)
_MEMORY_NODES = {
    "agenticmind.agents.conversation_history",
    "agenticmind.agents.save_turn",
    "agenticmind.agents.remember",
    "agenticmind.agents.recall",
    "agenticmind.agents.tool_memory",
}
_BASE_MEMORY_MB = 512
_LOCAL_MODEL_MEMORY_MB = 1536


@dataclass
class Requirements:
    endpoints: list[str]
    secrets: list[str]
    network_egress: bool
    #: reasons the app needs a persistent, writable data directory (empty = it does not)
    persistent_data: list[str] = field(default_factory=list)
    #: reasons the app keeps state in process memory (so it must run as a single replica)
    in_process_state: list[str] = field(default_factory=list)
    #: the slowest single node's execution limit on any endpoint: a request may legitimately take this long
    longest_request_seconds: float = 30.0
    slowest_node: str = ""
    local_models: list[str] = field(default_factory=list)
    external_services: list[str] = field(default_factory=list)
    pauses_for_humans: list[str] = field(default_factory=list)
    bundled_files: list[str] = field(default_factory=list)
    memory_mb: int = _BASE_MEMORY_MB
    gpu: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def derive_requirements(doc: ProjectDocument, registry: NodeRegistry, result: CompileResult) -> Requirements:
    req = result.manifest["requirements"]
    used = {nid for ep in result.endpoints for nid in ep.node_ids}
    persistent: list[str] = []
    state: list[str] = []
    pauses: list[str] = []
    longest, slowest = 0.0, ""
    for node in doc.nodes:
        if node.id not in used:
            continue
        entry = registry.get(node.type, node.version)
        if entry is None:
            continue
        manifest = entry.definition.manifest
        label = f"'{node.id}' ({manifest.display_name})"
        if manifest.execution.timeout_seconds > longest:
            longest, slowest = manifest.execution.timeout_seconds, label
        if node.type in _DISK_WRITERS:
            persistent.append(f"{label} builds a search index under DATA_DIR")
        if node.type in _MEMORY_NODES:
            state.append(f"{label} keeps conversations/memories in process memory")
        if manifest.can_suspend:
            pauses.append(label)
    for service in req["services"]:
        if "under DATA_DIR" in service:
            persistent.append(f"{service.split(' (')[0]} stores vectors under DATA_DIR")
    external = [s for s in req["services"] if "under DATA_DIR" not in s]
    return Requirements(
        endpoints=[ep.path for ep in result.endpoints],
        secrets=list(req["secrets"]),
        network_egress=bool(req["network"]) or bool(req["local_models"]),
        persistent_data=sorted(set(persistent)),
        in_process_state=sorted(set(state)),
        longest_request_seconds=longest or 30.0,
        slowest_node=slowest,
        local_models=list(req["local_models"]),
        external_services=external,
        pauses_for_humans=pauses,
        bundled_files=list(req["bundled_files"]),
        memory_mb=_BASE_MEMORY_MB + (_LOCAL_MODEL_MEMORY_MB if req["local_models"] else 0),
    )

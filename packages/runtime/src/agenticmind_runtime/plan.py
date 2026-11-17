"""Compile a validated IR document into an execution plan."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from agenticmind_ir import Edge, EdgeKind, Node, ProjectDocument, iter_secret_refs
from agenticmind_node_sdk import HANDLE_PORT_TYPES, NodeConfig, resolve_port_type
from agenticmind_registry import NodeRegistry, RegistryEntry, TrustLevel

from .diagnostics import Code, Diagnostic, ValidationReport
from .loops import analyze_loops, direct_members
from .secrets import SecretResolver
from .validation import DEFAULT_IN_PROCESS_TRUST, validate_document


class PlanError(Exception):
    def __init__(self, report: ValidationReport) -> None:
        super().__init__(f"Graph is invalid: {len(report.errors)} error(s)")
        self.report = report


@dataclass(frozen=True)
class PlannedNode:
    node: Node
    entry: RegistryEntry
    config: NodeConfig
    data_in: tuple[Edge, ...]
    control_in: tuple[Edge, ...]
    upstream: frozenset[str]
    timeout_seconds: float
    max_attempts: int
    allowed_secrets: frozenset[str]

    @property
    def id(self) -> str:
        return self.node.id


@dataclass(frozen=True)
class LoopRegion:
    """A loop body executed once per iteration by its loop end."""

    start: str
    end: str
    body: frozenset[str]
    """All body nodes, including nested loops (start and end excluded)."""
    order: tuple[str, ...]
    """Body nodes run directly in an iteration scope (nested loops collapsed to their start/end)."""


@dataclass(frozen=True)
class ExecutionPlan:
    project_id: str
    nodes: dict[str, PlannedNode]
    order: tuple[str, ...]
    """Deterministic topological order (ties broken by node ID)."""
    report: ValidationReport
    targets: tuple[str, ...] = ()
    """When non-empty, only these nodes and their upstream dependencies are executed."""
    regions: dict[str, LoopRegion] = field(default_factory=dict)
    """Loop bodies keyed by loop end id. ``order`` excludes all body nodes."""


def _topological_order(node_ids: list[str], edges: list[Edge]) -> list[str]:
    indeg = {n: 0 for n in node_ids}
    out: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        if e.target.node not in out[e.source.node]:
            out[e.source.node].add(e.target.node)
            indeg[e.target.node] += 1
    ready = sorted(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for t in sorted(out[n]):
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
        ready.sort()
    return order


def compile_plan(
    doc: ProjectDocument,
    registry: NodeRegistry,
    *,
    secrets: SecretResolver | None = None,
    in_process_trust: frozenset[TrustLevel] = DEFAULT_IN_PROCESS_TRUST,
    targets: Sequence[str] | None = None,
) -> ExecutionPlan:
    report = validate_document(doc, registry, secrets=secrets, in_process_trust=in_process_trust)
    if not report.valid:
        raise PlanError(report)

    included = {n.id for n in doc.nodes}
    if targets:
        unknown = [t for t in targets if t not in included]
        if unknown:
            raise PlanError(
                ValidationReport(
                    diagnostics=[
                        Diagnostic(code=Code.RUN_TARGET_UNKNOWN, node_id=t, message=f"Run target '{t}' is not a node")
                        for t in unknown
                    ]
                )
            )
        included = _ancestors(set(targets), list(doc.edges))
    nodes_by_id = {n.id: n for n in doc.nodes}

    data_in: dict[str, list[Edge]] = defaultdict(list)
    control_in: dict[str, list[Edge]] = defaultdict(list)
    for e in sorted(doc.edges, key=lambda e: e.id):
        (data_in if e.kind == EdgeKind.DATA else control_in)[e.target.node].append(e)

    planned: dict[str, PlannedNode] = {}
    for node in doc.nodes:
        if node.id not in included:
            continue
        entry = registry.get(node.type, node.version)
        assert entry is not None  # guaranteed by validation
        manifest = entry.definition.manifest
        overrides = node.execution
        ins = tuple(data_in[node.id])
        ctrl = tuple(control_in[node.id])
        refs: Any = frozenset()
        if manifest.permissions.credentials:
            # own config + references carried by handles wired into this node (transitively, so a tool
            # built from an embedding-model handle delegates that model's key to the agent using it)
            refs = frozenset(_handle_secret_refs(node.id, nodes_by_id, data_in, registry, include_self=True))
        planned[node.id] = PlannedNode(
            node=node,
            entry=entry,
            config=entry.definition.parse_config(node.config),
            data_in=ins,
            control_in=ctrl,
            upstream=frozenset(e.source.node for e in (*ins, *ctrl)),
            timeout_seconds=(overrides.timeout_seconds if overrides and overrides.timeout_seconds else None)
            or manifest.execution.timeout_seconds,
            max_attempts=(overrides.max_attempts if overrides and overrides.max_attempts else None)
            or manifest.execution.max_attempts,
            allowed_secrets=refs,
        )
    edges = [e for e in doc.edges if e.source.node in included and e.target.node in included]
    order = _topological_order([n.id for n in doc.nodes if n.id in included], edges)

    regions: dict[str, LoopRegion] = {}
    roles = {nid: pn.entry.definition.manifest.runtime_role for nid, pn in planned.items()}
    if any(roles.values()):
        analysis = analyze_loops(roles, edges)
        hidden: set[str] = set()
        for pair in analysis.pairs:
            members = direct_members(pair.body, analysis.pairs)
            regions[pair.end] = LoopRegion(
                start=pair.start,
                end=pair.end,
                body=pair.body,
                order=tuple(n for n in order if n in members),
            )
            # In its enclosing scope the loop end waits for the loop start and for every value the body
            # (or the end itself) reads from outside the loop.
            externals = {
                e.source.node
                for e in edges
                if (e.target.node in pair.body or e.target.node == pair.end)
                and e.source.node not in pair.body
                and e.source.node != pair.start
            }
            planned[pair.end] = replace(planned[pair.end], upstream=frozenset({pair.start} | externals))
            hidden |= pair.body
        order = [n for n in order if n not in hidden]

    return ExecutionPlan(
        project_id=doc.project.id,
        nodes=planned,
        order=tuple(order),
        report=report,
        targets=tuple(targets or ()),
        regions=regions,
    )


def _handle_secret_refs(
    node_id: str,
    nodes_by_id: dict[str, Node],
    data_in: dict[str, list[Edge]],
    registry: NodeRegistry,
    *,
    include_self: bool,
    seen: set[str] | None = None,
) -> set[str]:
    seen = seen if seen is not None else set()
    if node_id in seen:
        return set()
    seen.add(node_id)
    node = nodes_by_id[node_id]
    refs = set(iter_secret_refs(node.config)) if include_self else set()
    entry = registry.get(node.type, node.version)
    if entry is None:
        return refs
    for e in data_in.get(node_id, []):
        port = entry.definition.manifest.input(e.target.port)
        if port is not None and resolve_port_type(port, node.config).value in HANDLE_PORT_TYPES:
            refs |= _handle_secret_refs(e.source.node, nodes_by_id, data_in, registry, include_self=True, seen=seen)
    return refs


def _ancestors(targets: set[str], edges: list[Edge]) -> set[str]:
    """``targets`` plus every node they depend on through data or control edges."""
    upstream: dict[str, set[str]] = defaultdict(set)
    for e in edges:
        upstream[e.target.node].add(e.source.node)
    seen = set(targets)
    stack = list(targets)
    while stack:
        for parent in upstream[stack.pop()]:
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen

"""Graph validation pipeline (blueprint section 5.1).

1. parse against the IR schema
2. resolve node types/versions from the registry
3. validate node configuration
4. validate ports, types, cardinality and required inputs
5. validate graph structure (duplicates, cycles, isolated nodes)
6. check secrets and runtime capabilities (trust/isolation)
7. return structured diagnostics with stable codes and canvas references
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from agenticmind_ir import (
    CONTROL_TRIGGER_PORT,
    IR_VERSION,
    EdgeKind,
    IRLoadError,
    ProjectDocument,
    is_secret_ref,
    iter_secret_refs,
    load_project,
)
from agenticmind_node_sdk import Cardinality, effective_branches, is_compatible, resolve_port_type, secret_fields
from agenticmind_registry import NodeRegistry, RegistryEntry, TrustLevel
from pydantic import ValidationError

from .diagnostics import Code, Diagnostic, Severity, ValidationReport
from .loops import analyze_loops
from .secrets import SecretResolver

#: Trust levels allowed to run inside the runtime process. Anything else needs process/container
#: isolation, which is not implemented yet (ADR-0004).
DEFAULT_IN_PROCESS_TRUST: frozenset[TrustLevel] = frozenset({TrustLevel.CORE})


def parse_document(source: str | bytes | Mapping[str, Any]) -> tuple[ProjectDocument | None, ValidationReport]:
    try:
        return load_project(source), ValidationReport()
    except IRLoadError as exc:
        return None, ValidationReport(
            diagnostics=[
                Diagnostic(code=Code(i["code"]), message=i["message"], path=i["path"] or None) for i in exc.issues
            ]
        )


def find_cycle_nodes(node_ids: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str]:
    """Return IDs of nodes that participate in (or are downstream-locked by) a cycle (Kahn)."""
    ids = list(node_ids)
    indeg = {n: 0 for n in ids}
    out: dict[str, list[str]] = defaultdict(list)
    for s, t in edges:
        if s in indeg and t in indeg:
            out[s].append(t)
            indeg[t] += 1
    queue = [n for n in ids if indeg[n] == 0]
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for t in out[n]:
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    return sorted(n for n in ids if indeg[n] > 0) if seen < len(ids) else []


def validate_document(
    doc: ProjectDocument,
    registry: NodeRegistry,
    *,
    secrets: SecretResolver | None = None,
    in_process_trust: frozenset[TrustLevel] = DEFAULT_IN_PROCESS_TRUST,
) -> ValidationReport:
    diags: list[Diagnostic] = []
    add = diags.append

    if not doc.nodes:
        add(Diagnostic(code=Code.GRAPH_EMPTY, severity=Severity.WARNING, message="The graph has no nodes"))

    # --- duplicates ------------------------------------------------------------------------
    nodes_by_id: dict[str, Any] = {}
    for node in doc.nodes:
        if node.id in nodes_by_id:
            add(
                Diagnostic(code=Code.GRAPH_DUPLICATE_NODE_ID, node_id=node.id, message=f"Duplicate node ID '{node.id}'")
            )
        else:
            nodes_by_id[node.id] = node
    seen_edges: set[str] = set()
    for edge in doc.edges:
        if edge.id in seen_edges:
            add(
                Diagnostic(code=Code.GRAPH_DUPLICATE_EDGE_ID, edge_id=edge.id, message=f"Duplicate edge ID '{edge.id}'")
            )
        seen_edges.add(edge.id)

    # --- 2/3/6: registry, config, trust, secrets ---------------------------------------------
    entries: dict[str, RegistryEntry] = {}
    for node in nodes_by_id.values():
        if not registry.has_type(node.type):
            add(
                Diagnostic(
                    code=Code.NODE_UNKNOWN_TYPE,
                    node_id=node.id,
                    message=f"Unknown node type '{node.type}'",
                    hint="Install the node package that provides this type.",
                )
            )
            continue
        entry = registry.get(node.type, node.version)
        if entry is None:
            add(
                Diagnostic(
                    code=Code.NODE_UNKNOWN_VERSION,
                    node_id=node.id,
                    message=f"Version {node.version} of '{node.type}' is not installed",
                    hint=f"Available versions: {', '.join(registry.versions(node.type))}",
                )
            )
            continue
        entries[node.id] = entry
        manifest = entry.definition.manifest
        if IR_VERSION not in manifest.compatibility.ir_versions:
            add(
                Diagnostic(
                    code=Code.NODE_INCOMPATIBLE_IR,
                    node_id=node.id,
                    message=f"{node.type}@{node.version} does not support IR {IR_VERSION}",
                )
            )
        if entry.trust not in in_process_trust:
            add(
                Diagnostic(
                    code=Code.NODE_ISOLATION_UNAVAILABLE,
                    node_id=node.id,
                    message=f"'{node.type}' has trust level '{entry.trust.value}' and requires isolated execution, "
                    "which this runtime does not provide yet",
                    hint="Only core nodes run in-process. See docs/adr/0004-node-isolation.md.",
                )
            )

        secret_props = secret_fields(manifest.config_schema)
        for field in secret_props:
            value = node.config.get(field)
            if value is not None and not is_secret_ref(value):
                add(
                    Diagnostic(
                        code=Code.SECRET_LITERAL_VALUE,
                        node_id=node.id,
                        path=f"config.{field}",
                        message=f"'{field}' must be a secret reference, not a literal value",
                        hint='Use {"$secret": "NAME"} and set NAME in the environment.',
                    )
                )
        literal_secret_found = any(
            node.config.get(f) is not None and not is_secret_ref(node.config.get(f)) for f in secret_props
        )
        if not literal_secret_found:
            try:
                entry.definition.parse_config(node.config)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err["loc"])
                    add(
                        Diagnostic(
                            code=Code.CONFIG_INVALID,
                            node_id=node.id,
                            path=f"config.{loc}" if loc else "config",
                            message=f"{loc or 'config'}: {err['msg']}",
                        )
                    )

        refs = sorted(set(iter_secret_refs(node.config)))
        if refs and not manifest.permissions.credentials:
            add(
                Diagnostic(
                    code=Code.SECRET_NOT_PERMITTED,
                    node_id=node.id,
                    message=f"'{node.type}' does not declare the credentials permission but references secrets",
                )
            )
        if secrets is not None:
            for name in refs:
                if secrets.get(name) is None:
                    add(
                        Diagnostic(
                            code=Code.SECRET_NOT_CONFIGURED,
                            severity=Severity.WARNING,
                            node_id=node.id,
                            message=f"Secret '{name}' is not configured in this environment",
                        )
                    )

    # --- 4: edges and ports --------------------------------------------------------------------
    data_in: dict[tuple[str, str], list[str]] = defaultdict(list)
    connections: set[tuple[str, str, str, str, str]] = set()
    graph_edges: list[tuple[str, str]] = []
    connected: set[str] = set()
    for edge in doc.edges:
        s, t = edge.source, edge.target
        missing = [r.node for r in (s, t) if r.node not in nodes_by_id]
        if missing:
            add(
                Diagnostic(
                    code=Code.EDGE_UNKNOWN_NODE,
                    edge_id=edge.id,
                    message=f"Edge references unknown node(s): {', '.join(missing)}",
                )
            )
            continue
        if s.node == t.node:
            add(
                Diagnostic(
                    code=Code.EDGE_SELF_LOOP, edge_id=edge.id, node_id=s.node, message="Edge connects a node to itself"
                )
            )
            continue
        key = (edge.kind.value, s.node, s.port, t.node, t.port)
        if key in connections:
            add(
                Diagnostic(
                    code=Code.EDGE_DUPLICATE,
                    severity=Severity.WARNING,
                    edge_id=edge.id,
                    message="Duplicate connection between the same ports",
                )
            )
        connections.add(key)
        graph_edges.append((s.node, t.node))
        connected.update((s.node, t.node))

        src_entry, tgt_entry = entries.get(s.node), entries.get(t.node)
        if edge.kind == EdgeKind.CONTROL:
            branches = effective_branches(src_entry.manifest, nodes_by_id[s.node].config) if src_entry else []
            if src_entry and s.port not in branches:
                add(
                    Diagnostic(
                        code=Code.EDGE_UNKNOWN_PORT,
                        edge_id=edge.id,
                        node_id=s.node,
                        port=s.port,
                        message=f"'{s.node}' has no control branch '{s.port}' (branches: {', '.join(branches)})",
                    )
                )
            if t.port != CONTROL_TRIGGER_PORT:
                add(
                    Diagnostic(
                        code=Code.EDGE_INVALID_CONTROL_TARGET,
                        edge_id=edge.id,
                        node_id=t.node,
                        port=t.port,
                        message=f"Control edges must target the '{CONTROL_TRIGGER_PORT}' port",
                    )
                )
            continue

        # data edge
        out_port = src_entry.manifest.output(s.port) if src_entry else None
        in_port = tgt_entry.manifest.input(t.port) if tgt_entry else None
        if src_entry and out_port is None:
            add(
                Diagnostic(
                    code=Code.EDGE_UNKNOWN_PORT,
                    edge_id=edge.id,
                    node_id=s.node,
                    port=s.port,
                    message=f"'{s.node}' has no output port '{s.port}'",
                )
            )
        if tgt_entry and in_port is None:
            add(
                Diagnostic(
                    code=Code.EDGE_UNKNOWN_PORT,
                    edge_id=edge.id,
                    node_id=t.node,
                    port=t.port,
                    message=f"'{t.node}' has no input port '{t.port}'",
                )
            )
        if in_port is not None:
            data_in[(t.node, t.port)].append(edge.id)
        if out_port is not None and in_port is not None:
            st = resolve_port_type(out_port, nodes_by_id[s.node].config)
            tt = resolve_port_type(in_port, nodes_by_id[t.node].config)
            if not is_compatible(st, tt):
                add(
                    Diagnostic(
                        code=Code.EDGE_TYPE_MISMATCH,
                        edge_id=edge.id,
                        node_id=t.node,
                        port=t.port,
                        message=f"Cannot connect {st.value} output '{s.node}.{s.port}' to {tt.value} input "
                        f"'{t.node}.{t.port}'",
                        hint="Insert an explicit conversion node (e.g. To Text, Parse JSON).",
                    )
                )

    for node_id, entry in entries.items():
        for port in entry.manifest.inputs:
            incoming = data_in.get((node_id, port.name), [])
            if port.required and not incoming:
                add(
                    Diagnostic(
                        code=Code.PORT_MISSING_REQUIRED_INPUT,
                        node_id=node_id,
                        port=port.name,
                        message=f"Required input '{port.name}' of '{node_id}' is not connected",
                    )
                )
            if port.cardinality == Cardinality.SINGLE and len(incoming) > 1:
                add(
                    Diagnostic(
                        code=Code.PORT_CARDINALITY_EXCEEDED,
                        node_id=node_id,
                        port=port.name,
                        message=f"Input '{port.name}' of '{node_id}' accepts one connection, has {len(incoming)}",
                        hint="Use a Collect node to join several values explicitly.",
                    )
                )

    # --- 5: structure ---------------------------------------------------------------------------
    cycle = find_cycle_nodes(nodes_by_id.keys(), graph_edges)
    if cycle:
        add(
            Diagnostic(
                code=Code.GRAPH_CYCLE,
                node_id=cycle[0],
                related_node_ids=cycle,
                message=f"The graph contains a cycle involving: {', '.join(cycle)}",
                hint="Accidental cycles are rejected; iterative execution will use explicit Loop nodes.",
            )
        )
    if not cycle:
        roles = {nid: e.manifest.runtime_role for nid, e in entries.items()}
        if any(roles.values()):
            loops = analyze_loops(
                roles, [e for e in doc.edges if e.source.node in entries and e.target.node in entries]
            )
            for start in loops.unpaired_starts:
                add(
                    Diagnostic(
                        code=Code.LOOP_UNPAIRED,
                        node_id=start,
                        message=f"'{start}' needs a matching loop partner connected through its 'scope' port",
                        hint="Connect Loop.scope → Loop End.scope; the nodes in between form the loop body.",
                    )
                )
            for start in loops.shared_scopes:
                add(
                    Diagnostic(
                        code=Code.LOOP_SCOPE_SHARED,
                        node_id=start,
                        message=f"Loop '{start}' is connected to more than one Loop End",
                    )
                )
            for start, edge in loops.leaks:
                add(
                    Diagnostic(
                        code=Code.LOOP_REGION_LEAK,
                        node_id=edge.target.node,
                        edge_id=edge.id,
                        related_node_ids=[start, edge.source.node],
                        message=f"'{edge.target.node}' uses a value produced inside loop '{start}' "
                        f"('{edge.source.node}') but is not part of the loop body",
                        hint="Send results out of a loop through its Loop End (results / last).",
                    )
                )
            for a, b in loops.overlaps:
                add(
                    Diagnostic(
                        code=Code.LOOP_OVERLAP,
                        node_id=a,
                        related_node_ids=[a, b],
                        message=f"Loops '{a}' and '{b}' overlap; loops must be separate or fully nested",
                    )
                )
            for pair in loops.pairs:
                for node_id in sorted(pair.body):
                    if entries[node_id].manifest.can_suspend:
                        add(
                            Diagnostic(
                                code=Code.SUSPEND_IN_LOOP,
                                node_id=node_id,
                                related_node_ids=[pair.start, pair.end],
                                message=f"'{node_id}' can pause the run, which is not supported inside "
                                f"loop '{pair.start}'",
                                hint="Move the approval before or after the loop (e.g. approve Loop End's results).",
                            )
                        )
    if len(nodes_by_id) > 1:
        for node_id in nodes_by_id:
            if node_id not in connected:
                add(
                    Diagnostic(
                        code=Code.GRAPH_ISOLATED_NODE,
                        severity=Severity.WARNING,
                        node_id=node_id,
                        message=f"'{node_id}' is not connected to anything",
                    )
                )

    return ValidationReport(diagnostics=diags)


def validate_project(
    source: ProjectDocument | str | bytes | Mapping[str, Any],
    registry: NodeRegistry,
    **kwargs: Any,
) -> tuple[ProjectDocument | None, ValidationReport]:
    """Full pipeline from raw input. Returns the parsed document (if parseable) and the report."""
    if isinstance(source, ProjectDocument):
        doc = source
    else:
        doc, report = parse_document(source)
        if doc is None:
            return None, report
    return doc, validate_document(doc, registry, **kwargs)

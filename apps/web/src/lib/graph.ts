// Conversions between the canonical IR and React Flow's view model.
// The IR is the source of truth: the canvas is a projection of it, and every edit is
// written back to IR nodes/edges (positions live in node.ui, which never affects execution).

import type { Connection, Edge, Node } from "@xyflow/react";
import { MarkerType } from "@xyflow/react";

import type {
  InputPortSpec,
  IREdge,
  IRNode,
  JsonValue,
  NodeManifest,
  OutputPortSpec,
  ProjectDocument,
} from "../api/types";

export interface AmNodeData extends Record<string, unknown> {
  ir: IRNode;
}
export type AmFlowNode = Node<AmNodeData, "am">;
export interface AmEdgeData extends Record<string, unknown> {
  kind: "data" | "control";
}
export type AmFlowEdge = Edge<AmEdgeData>;

export const PORT_TYPES = new Set([
  "Any", "String", "Number", "Boolean", "JSON", "Document", "DocumentSet", "EmbeddingVector",
  "VectorStoreHandle", "Database", "ChatMessage", "Tool", "AgentState", "EvaluationDataset", "HTTPRequest", "HTTPResponse",
]);
const JSON_SUBTYPES = new Set(["String", "Number", "Boolean", "JSON"]);

export const CTRL_IN = "ctrl-in";
export const CONTROL_TRIGGER_PORT = "trigger";

export const manifestKey = (type: string, version: string) => `${type}@${version}`;
export const dataIn = (port: string) => `in:${port}`;
export const dataOut = (port: string) => `out:${port}`;
export const ctrlOut = (branch: string) => `ctrl-out:${branch}`;

export type ParsedHandle =
  | { kind: "data"; dir: "in" | "out"; port: string }
  | { kind: "control"; dir: "in" | "out"; port: string };

export function parseHandle(id: string | null | undefined): ParsedHandle | null {
  if (!id) return null;
  if (id === CTRL_IN) return { kind: "control", dir: "in", port: CONTROL_TRIGGER_PORT };
  if (id.startsWith("ctrl-out:")) return { kind: "control", dir: "out", port: id.slice(9) };
  if (id.startsWith("in:")) return { kind: "data", dir: "in", port: id.slice(3) };
  if (id.startsWith("out:")) return { kind: "data", dir: "out", port: id.slice(4) };
  return null;
}

/** Mirrors agenticmind_node_sdk.manifest.effective_branches (e.g. Router routes + "default"). */
export function effectiveBranches(manifest: NodeManifest, config: Record<string, JsonValue>): string[] {
  const field = manifest.branches_from_config;
  if (!field) return manifest.branches;
  let raw: unknown = config[field];
  if (raw === undefined || raw === null) raw = manifest.config_schema.properties?.[field]?.default ?? [];
  const names: string[] = [];
  for (const item of Array.isArray(raw) ? raw : []) {
    const name = typeof item === "string" ? item : (item as { name?: unknown } | null)?.name;
    if (typeof name === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(name) && !names.includes(name)) names.push(name);
  }
  return [...names, ...manifest.branches.filter((b) => !names.includes(b))];
}

/** Mirrors agenticmind_node_sdk.types.is_compatible (strict; subtype acceptance only). */
export function isCompatible(source: string, target: string): boolean {
  if (target === "Any" || source === target) return true;
  return target === "JSON" && JSON_SUBTYPES.has(source);
}

/** Mirrors agenticmind_node_sdk.manifest.resolve_port_type. */
export function effectivePortType(
  port: InputPortSpec | OutputPortSpec,
  config: Record<string, JsonValue>,
): string {
  if (port.type_from_config) {
    const v = config[port.type_from_config];
    if (typeof v === "string" && PORT_TYPES.has(v)) return v;
  }
  return port.type;
}

export function edgeStyle(kind: "data" | "control", branch?: string): Partial<AmFlowEdge> {
  if (kind === "control") {
    return {
      style: { stroke: "#b9975c", strokeWidth: 1.5, strokeDasharray: "6 4" },
      markerEnd: { type: MarkerType.ArrowClosed, color: "#b9975c" },
      label: branch && branch !== "done" ? branch : undefined,
      labelStyle: { fill: "#8a6a32", fontWeight: 600, fontSize: 11 },
      labelBgStyle: { fill: "#faf6ee" },
    };
  }
  return { style: { stroke: "#8b909c", strokeWidth: 1.5 } };
}

export function irEdgeToFlow(e: IREdge): AmFlowEdge {
  const isControl = e.kind === "control";
  return {
    id: e.id,
    source: e.source.node,
    target: e.target.node,
    sourceHandle: isControl ? ctrlOut(e.source.port) : dataOut(e.source.port),
    targetHandle: isControl ? CTRL_IN : dataIn(e.target.port),
    data: { kind: e.kind },
    ...edgeStyle(e.kind, e.source.port),
  };
}

export function flowEdgeToIR(e: AmFlowEdge): IREdge | null {
  const s = parseHandle(e.sourceHandle);
  const t = parseHandle(e.targetHandle);
  if (!s || !t || s.kind !== t.kind) return null;
  return {
    id: e.id,
    kind: s.kind,
    source: { node: e.source, port: s.port },
    target: { node: e.target, port: t.port },
  };
}

export function docToFlow(doc: ProjectDocument): { nodes: AmFlowNode[]; edges: AmFlowEdge[] } {
  const nodes: AmFlowNode[] = doc.nodes.map((n, i) => ({
    id: n.id,
    type: "am",
    position: n.ui?.position ?? { x: (i % 4) * 300, y: Math.floor(i / 4) * 200 },
    data: { ir: n },
  }));
  return { nodes, edges: doc.edges.map(irEdgeToFlow) };
}

export function flowToDoc(
  base: ProjectDocument,
  meta: { name: string; description: string },
  nodes: AmFlowNode[],
  edges: AmFlowEdge[],
): ProjectDocument {
  const irNodes: IRNode[] = nodes.map((n) => {
    const ir = n.data.ir;
    const ui: NonNullable<IRNode["ui"]> = {
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    };
    if (ir.ui?.label) ui.label = ir.ui.label;
    const out: IRNode = { id: ir.id, type: ir.type, version: ir.version, config: ir.config, ui };
    if (ir.execution && (ir.execution.timeout_seconds || ir.execution.max_attempts)) {
      out.execution = ir.execution;
    }
    return out;
  });
  const irEdges = edges.map(flowEdgeToIR).filter((e): e is IREdge => e !== null);
  return {
    ...base,
    project: { ...base.project, name: meta.name, description: meta.description },
    nodes: irNodes,
    edges: irEdges,
  };
}

export function uniqueNodeId(type: string, existing: Iterable<string>): string {
  const taken = new Set(existing);
  const base = (type.split(".").pop() ?? "node").replace(/[^A-Za-z0-9_]/g, "_");
  for (let i = 1; ; i++) {
    const id = `${base}_${i}`;
    if (!taken.has(id)) return id;
  }
}

export function uniqueEdgeId(existing: Iterable<string>): string {
  const taken = new Set(existing);
  for (;;) {
    const id = `e_${Math.random().toString(36).slice(2, 10)}`;
    if (!taken.has(id)) return id;
  }
}

/** Explicit defaults make the IR self-describing and reproducible. */
export function defaultConfig(manifest: NodeManifest): Record<string, JsonValue> {
  const config: Record<string, JsonValue> = {};
  for (const [key, prop] of Object.entries(manifest.config_schema.properties ?? {})) {
    if (prop.default !== undefined && prop.default !== null) config[key] = prop.default as JsonValue;
  }
  return config;
}

export type ConnectionCheck = { ok: true; kind: "data" | "control" } | { ok: false; reason: string };

/** Client-side mirror of the most common validator rules, used to give instant feedback while wiring. */
export function checkConnection(
  conn: Connection | AmFlowEdge,
  nodes: AmFlowNode[],
  edges: AmFlowEdge[],
  manifests: Map<string, NodeManifest>,
): ConnectionCheck {
  if (conn.source === conn.target) return { ok: false, reason: "A node cannot connect to itself" };
  const s = parseHandle(conn.sourceHandle);
  const t = parseHandle(conn.targetHandle);
  if (!s || !t || s.dir !== "out" || t.dir !== "in") return { ok: false, reason: "Connect an output to an input" };
  if (s.kind !== t.kind) return { ok: false, reason: "Data and control ports cannot be mixed" };
  if (s.kind === "control") return { ok: true, kind: "control" };

  const sn = nodes.find((n) => n.id === conn.source);
  const tn = nodes.find((n) => n.id === conn.target);
  const sm = sn && manifests.get(manifestKey(sn.data.ir.type, sn.data.ir.version));
  const tm = tn && manifests.get(manifestKey(tn.data.ir.type, tn.data.ir.version));
  const op = sm?.outputs.find((p) => p.name === s.port);
  const ip = tm?.inputs.find((p) => p.name === t.port);
  if (!sn || !tn || !op || !ip) return { ok: false, reason: "Unknown port" };
  const st = effectivePortType(op, sn.data.ir.config);
  const tt = effectivePortType(ip, tn.data.ir.config);
  if (!isCompatible(st, tt)) return { ok: false, reason: `${st} → ${tt} needs a conversion node` };
  if (ip.cardinality === "single") {
    const taken = edges.some((e) => e.target === conn.target && e.targetHandle === conn.targetHandle);
    if (taken) return { ok: false, reason: `Input '${ip.name}' already has a connection` };
  }
  return { ok: true, kind: "data" };
}

export function blankDocument(id: string, name: string): ProjectDocument {
  return {
    ir_version: "1.0",
    project: { id, name, description: "" },
    runtime: { engine: "agenticmind", profile: "development" },
    nodes: [],
    edges: [],
    metadata: {},
  };
}

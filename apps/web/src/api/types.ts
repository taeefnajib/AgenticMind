// Mirrors of the backend contracts. The Python Pydantic models are authoritative
// (packages/ir, packages/node-sdk, packages/runtime); keep these in sync.

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export type Mode = "no_code" | "low_code" | "developer";

export interface InputPortSpec {
  name: string;
  type: string;
  required: boolean;
  cardinality: "single" | "multiple";
  description: string;
  type_from_config: string | null;
}

export interface OutputPortSpec {
  name: string;
  type: string;
  description: string;
  type_from_config: string | null;
}

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  default?: unknown;
  enum?: (string | number)[];
  const?: string | number;
  minimum?: number;
  maximum?: number;
  pattern?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  anyOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  "x-ui"?: { widget?: string; advanced?: boolean; placeholder?: string };
  "x-secret"?: boolean;
}

export interface NodeManifest {
  type: string;
  version: string;
  display_name: string;
  category: string;
  package: string;
  description: string;
  inputs: InputPortSpec[];
  outputs: OutputPortSpec[];
  branches: string[];
  config_schema: JsonSchema;
  permissions: Record<string, boolean>;
  execution: { timeout_seconds: number; max_attempts: number; idempotent: boolean };
  modes: Mode[];
  tags: string[];
  trust: string;
  source: string;
}

export interface IRNode {
  id: string;
  type: string;
  version: string;
  config: Record<string, JsonValue>;
  execution?: { timeout_seconds?: number | null; max_attempts?: number | null };
  ui?: { position?: { x: number; y: number } | null; label?: string | null };
}

export interface IREdge {
  id: string;
  kind: "data" | "control";
  source: { node: string; port: string };
  target: { node: string; port: string };
}

export interface ProjectDocument {
  ir_version: "1.0";
  project: { id: string; name: string; description: string };
  runtime: { engine: "agenticmind"; profile: "development" | "production" | "enterprise" };
  nodes: IRNode[];
  edges: IREdge[];
  metadata: Record<string, JsonValue>;
}

export interface Diagnostic {
  code: string;
  severity: "error" | "warning" | "info";
  message: string;
  node_id?: string | null;
  edge_id?: string | null;
  port?: string | null;
  path?: string | null;
  related_node_ids?: string[];
  hint?: string | null;
}

export interface ValidationResult {
  valid: boolean;
  diagnostics: Diagnostic[];
}

export interface ProjectSummary {
  id: string;
  name: string;
  description: string;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProjectOut extends ProjectSummary {
  content_hash: string;
  document: ProjectDocument;
}

export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface RunError {
  code: string;
  message: string;
  node_id?: string | null;
  retryable?: boolean;
}

export interface RunOut {
  id: string;
  project_id: string;
  revision: number;
  status: RunStatus;
  inputs: Record<string, JsonValue>;
  outputs: Record<string, JsonValue> | null;
  error: RunError | null;
  node_status: Record<string, string> | null;
  content_hash: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface TraceEvent {
  run_id: string;
  project_id: string;
  seq: number;
  timestamp: string;
  event_type: string;
  status?: string;
  node_id?: string;
  node_type?: string;
  invocation_id?: string;
  attempt?: number;
  retry_count?: number;
  duration_ms?: number;
  inputs_summary?: unknown;
  outputs_summary?: unknown;
  usage?: {
    provider: string;
    model: string;
    input_tokens: number;
    output_tokens: number;
    estimated_cost_usd?: number | null;
  };
  error?: RunError;
  data?: Record<string, unknown>;
}

export interface TemplateInfo {
  name: string;
  title: string;
  description: string;
  node_count: number;
}

export type NodeRunStatus = "pending" | "running" | "retrying" | "succeeded" | "failed" | "skipped" | "cancelled";

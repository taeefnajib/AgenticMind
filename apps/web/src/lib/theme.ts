import type { NodeRunStatus } from "../api/types";

// Muted, low-saturation category colours: enough to tell node families apart, never loud.
export const CATEGORY_STYLE: Record<string, { color: string; label: string }> = {
  input: { color: "#5fae97", label: "Inputs & triggers" },
  data: { color: "#5b9fb3", label: "Data ingestion" },
  rag: { color: "#8a7be0", label: "RAG & knowledge" },
  models: { color: "#c6879a", label: "Models" },
  agents: { color: "#7f74d4", label: "Agents" },
  tools: { color: "#6aa39a", label: "Agent tools" },
  memory: { color: "#9a7fb8", label: "Memory" },
  human: { color: "#c08a5a", label: "Human in the loop" },
  semantic: { color: "#5b7fa8", label: "Data & semantics" },
  transform: { color: "#9b8cc9", label: "Transform" },
  convert: { color: "#8b909c", label: "Conversion" },
  control: { color: "#c4a165", label: "Control flow" },
  output: { color: "#6a98cf", label: "Outputs" },
};

export const categoryStyle = (category: string) => CATEGORY_STYLE[category] ?? { color: "#8b909c", label: category };

/** Soft tinted chip style for a category colour. */
export const chipStyle = (color: string) => ({ color, backgroundColor: `${color}1f` });

export const CATEGORY_ORDER = ["input", "data", "rag", "models", "agents", "tools", "memory", "semantic", "transform", "convert", "control", "human", "output"];

const PORT_COLORS: Record<string, string> = {
  String: "#4f7fd0",
  Number: "#c9823f",
  Boolean: "#b9668f",
  JSON: "#3f998b",
  Document: "#7a67cf",
  DocumentSet: "#7a67cf",
  Image: "#c0577a",
  EmbeddingModel: "#a8678a",
  VectorStoreHandle: "#4a8bad",
  Tool: "#3f9483",
  LoopScope: "#b08a4f",
  Database: "#5b7fa8",
  Ontology: "#7a6aa8",
  Any: "#7d8190",
};

// Lighter variants keep port labels readable on dark surfaces.
const PORT_COLORS_DARK: Record<string, string> = {
  String: "#8eaeea",
  Number: "#e2a76e",
  Boolean: "#d993b6",
  JSON: "#72c2b4",
  Document: "#a99bef",
  DocumentSet: "#a99bef",
  Image: "#ec8fae",
  EmbeddingModel: "#d39abb",
  VectorStoreHandle: "#83b8d6",
  Tool: "#7cc4b4",
  LoopScope: "#d8b47a",
  Database: "#93b4d8",
  Ontology: "#b8a8e0",
  Any: "#a4a8b4",
};
export const portColor = (type: string, dark = false) =>
  (dark ? PORT_COLORS_DARK[type] : PORT_COLORS[type]) ?? (dark ? "#a4a8b4" : "#7d8190");

export const STATUS_STYLE: Record<NodeRunStatus, { ring: string; badge: string; text: string }> = {
  pending: { ring: "", badge: "bg-slate-100 text-slate-500", text: "pending" },
  running: { ring: "ring-2 ring-accent-400 am-running", badge: "bg-accent-100 text-accent-700", text: "running" },
  retrying: { ring: "ring-2 ring-amber-300 am-running", badge: "bg-amber-100 text-amber-800", text: "retrying" },
  succeeded: { ring: "ring-1 ring-emerald-500/50", badge: "bg-emerald-100 text-emerald-700", text: "succeeded" },
  failed: { ring: "ring-2 ring-red-500", badge: "bg-red-100 text-red-700", text: "failed" },
  skipped: { ring: "ring-1 ring-slate-300 opacity-60", badge: "bg-slate-100 text-slate-500", text: "skipped" },
  cancelled: { ring: "ring-1 ring-slate-400 opacity-70", badge: "bg-slate-200 text-slate-600", text: "cancelled" },
  waiting: { ring: "ring-2 ring-amber-400", badge: "bg-amber-100 text-amber-800", text: "waiting for you" },
};

export const MODE_LABEL = { no_code: "No-code", low_code: "Low-code", developer: "Developer" } as const;

import type { NodeRunStatus } from "../api/types";

export const CATEGORY_STYLE: Record<string, { bar: string; chip: string; label: string }> = {
  input: { bar: "bg-emerald-500", chip: "bg-emerald-50 text-emerald-700", label: "Inputs & triggers" },
  output: { bar: "bg-sky-500", chip: "bg-sky-50 text-sky-700", label: "Outputs" },
  transform: { bar: "bg-violet-500", chip: "bg-violet-50 text-violet-700", label: "Transform" },
  convert: { bar: "bg-slate-500", chip: "bg-slate-100 text-slate-700", label: "Conversion" },
  control: { bar: "bg-amber-500", chip: "bg-amber-50 text-amber-800", label: "Control flow" },
  models: { bar: "bg-rose-500", chip: "bg-rose-50 text-rose-700", label: "Models" },
};

export const categoryStyle = (category: string) =>
  CATEGORY_STYLE[category] ?? { bar: "bg-slate-400", chip: "bg-slate-100 text-slate-700", label: category };

export const CATEGORY_ORDER = ["input", "transform", "convert", "control", "models", "output"];

const PORT_COLORS: Record<string, string> = {
  String: "#2563eb",
  Number: "#ea580c",
  Boolean: "#db2777",
  JSON: "#0d9488",
  Any: "#64748b",
};
export const portColor = (type: string) => PORT_COLORS[type] ?? "#4f46e5";

export const STATUS_STYLE: Record<NodeRunStatus, { ring: string; badge: string; text: string }> = {
  pending: { ring: "", badge: "bg-slate-100 text-slate-500", text: "pending" },
  running: { ring: "ring-2 ring-sky-400 am-running", badge: "bg-sky-100 text-sky-700", text: "running" },
  retrying: { ring: "ring-2 ring-amber-400 am-running", badge: "bg-amber-100 text-amber-800", text: "retrying" },
  succeeded: { ring: "ring-2 ring-emerald-400", badge: "bg-emerald-100 text-emerald-700", text: "succeeded" },
  failed: { ring: "ring-2 ring-red-500", badge: "bg-red-100 text-red-700", text: "failed" },
  skipped: { ring: "ring-1 ring-slate-300 opacity-60", badge: "bg-slate-100 text-slate-500", text: "skipped" },
  cancelled: { ring: "ring-1 ring-slate-400 opacity-70", badge: "bg-slate-200 text-slate-600", text: "cancelled" },
};

export const MODE_LABEL = { no_code: "No-code", low_code: "Low-code", developer: "Developer" } as const;

// Helpers for the Evaluation Lab (formatting and the plain-text editing formats for case fields).

import type { EvalCase, ExpectedSource } from "../api/types";

/** "file.md#Section", "file.md" or a chunk id: mirrors ExpectedSource.parse on the server. */
export function parseSource(text: string): ExpectedSource | null {
  const t = text.trim();
  if (!t) return null;
  if (t.startsWith("art_")) return { id: t };
  if (t.includes("#")) {
    const [filename, ...rest] = t.split("#");
    return { filename: filename!.trim() || null, section: rest.join("#").trim() || null };
  }
  return t.includes(".") ? { filename: t } : { id: t };
}

export function sourceLabel(s: ExpectedSource): string {
  if (s.id) return s.id;
  return `${s.filename ?? "*"}${s.section ? `#${s.section}` : ""}`;
}

export const splitLines = (text: string): string[] =>
  text
    .split(/\n|;/)
    .map((x) => x.trim())
    .filter(Boolean);

export function formatMetric(value: number | null | undefined, name: string, unit = ""): string {
  if (value === null || value === undefined) return "—";
  if (unit === "ms" || name === "latency_ms") return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
  if (unit === "USD" || name === "cost_usd") return `$${value.toFixed(value < 0.01 ? 5 : 3)}`;
  if (name === "tokens" || name === "retries") return value.toFixed(value % 1 ? 1 : 0);
  return `${(value * 100).toFixed(0)}%`;
}

export function formatDelta(delta: number | null, name: string, unit = ""): string {
  if (delta === null) return "";
  const sign = delta > 0 ? "+" : delta < 0 ? "−" : "±";
  const abs = Math.abs(delta);
  if (unit === "ms" || name === "latency_ms") return `${sign}${Math.round(abs)} ms`;
  if (unit === "USD" || name === "cost_usd") return `${sign}$${abs.toFixed(5)}`;
  if (name === "tokens" || name === "retries") return `${sign}${abs.toFixed(abs % 1 ? 1 : 0)}`;
  return `${sign}${(abs * 100).toFixed(0)} pts`;
}

export function newCase(existing: EvalCase[], inputKeys: string[]): EvalCase {
  const ids = new Set(existing.map((c) => c.id));
  let n = existing.length + 1;
  while (ids.has(`case_${n}`)) n++;
  return { id: `case_${n}`, inputs: Object.fromEntries(inputKeys.map((k) => [k, ""])) };
}

/** Drop empty optional fields so saved datasets stay small and hash-stable. */
export function cleanCase(c: EvalCase): EvalCase {
  const out: EvalCase = { id: c.id.trim(), inputs: c.inputs };
  if (c.reference?.trim()) out.reference = c.reference.trim();
  if (c.expected_sources?.length) out.expected_sources = c.expected_sources;
  if (c.must_include?.length) out.must_include = c.must_include;
  if (c.must_not_include?.length) out.must_not_include = c.must_not_include;
  if (c.expected_tools?.length) out.expected_tools = c.expected_tools;
  if (c.tags?.length) out.tags = c.tags;
  return out;
}

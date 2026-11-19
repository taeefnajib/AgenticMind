import type { AssistantChanges, Diagnostic } from "../api/types";

/** Short, human summary of a proposed graph change, e.g. "3 nodes added · 1 changed · 2 connections added". */
export function describeChanges(c: AssistantChanges): string {
  const parts: string[] = [];
  const n = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
  if (c.added?.length) parts.push(`${n(c.added.length, "node")} added`);
  if (c.modified?.length) parts.push(`${c.modified.length} changed`);
  if (c.removed?.length) parts.push(`${c.removed.length} removed`);
  if (c.edges_added) parts.push(`${n(c.edges_added, "connection")} added`);
  if (c.edges_removed) parts.push(`${n(c.edges_removed, "connection")} removed`);
  return parts.length ? parts.join(" · ") : "No changes to the graph";
}

/** Diagnostics about this machine (not the graph): shown as notes, they do not block applying. */
export const ENVIRONMENT_CODES = new Set(["SECRET_NOT_CONFIGURED"]);

export function splitDiagnostics(diags: Diagnostic[]): { blocking: Diagnostic[]; notes: Diagnostic[] } {
  const blocking = diags.filter((d) => d.severity === "error" && !ENVIRONMENT_CODES.has(d.code));
  const notes = diags.filter((d) => !blocking.includes(d) && d.severity !== "info");
  return { blocking, notes };
}

/** "agenticmind.models.llm" → "llm"; package types keep their namespace for clarity. */
export function shortType(type: string): string {
  return type.startsWith("agenticmind.") ? type.split(".").slice(2).join(".") : type;
}

export const SUGGESTIONS_EMPTY = [
  "Create a workflow that takes any question and answers only yes or no",
  "Build a pipeline that summarises a pasted article in three bullet points",
  "Route support tickets to billing, technical or general and draft a reply",
];

export const SUGGESTIONS_EXISTING = [
  "Explain what this workflow does",
  "Add a step that translates the final answer to French",
  "Make the answer shorter and more formal",
];

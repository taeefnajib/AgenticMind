import { useEffect, useState } from "react";

import type { JsonValue } from "../api/types";

/** One Model Router route as stored in the node config. */
export interface RouteValue {
  name: string;
  description?: string;
  keywords?: string[];
  model: string;
  system_prompt?: string;
}

export function asRoutes(value: JsonValue | undefined): RouteValue[] {
  if (!Array.isArray(value)) return [];
  return value.filter((r): r is Record<string, JsonValue> => !!r && typeof r === "object" && !Array.isArray(r)).map((r) => ({
    name: typeof r.name === "string" ? r.name : "",
    description: typeof r.description === "string" ? r.description : "",
    keywords: Array.isArray(r.keywords) ? r.keywords.filter((k): k is string => typeof k === "string") : [],
    model: typeof r.model === "string" ? r.model : "",
    ...(typeof r.system_prompt === "string" && r.system_prompt ? { system_prompt: r.system_prompt } : {}),
  }));
}

/** "a, b ,, c" → ["a", "b", "c"] */
export function parseKeywords(text: string): string[] {
  return text
    .split(",")
    .map((k) => k.trim())
    .filter(Boolean);
}

/** A free name in the style the backend accepts (lowercase, digits, - and _). */
export function nextRouteName(routes: RouteValue[]): string {
  const taken = new Set(routes.map((r) => r.name));
  for (let i = routes.length + 1; ; i++) if (!taken.has(`route-${i}`)) return `route-${i}`;
}

function toJson(routes: RouteValue[]): JsonValue {
  return routes.map((r) => {
    const out: Record<string, JsonValue> = { name: r.name, model: r.model };
    if (r.description) out.description = r.description;
    if (r.keywords && r.keywords.length) out.keywords = r.keywords;
    if (r.system_prompt) out.system_prompt = r.system_prompt;
    return out;
  });
}

/** Comma-separated keywords, committed on blur so typing a comma doesn't fight the input. */
function KeywordsInput({ value, onCommit }: { value: string[]; onCommit: (k: string[]) => void }) {
  const joined = value.join(", ");
  const [text, setText] = useState(joined);
  useEffect(() => setText(joined), [joined]);
  return (
    <input
      className="am-input !py-1 text-xs"
      value={text}
      placeholder="code, python, bug"
      onChange={(e) => setText(e.target.value)}
      onBlur={() => onCommit(parseKeywords(text))}
      aria-label="Keywords"
    />
  );
}

export function RoutesControl({ value, onChange }: { value: JsonValue | undefined; onChange: (v: JsonValue) => void }) {
  const routes = asRoutes(value);
  const [openPrompt, setOpenPrompt] = useState<number | null>(null);
  const update = (i: number, patch: Partial<RouteValue>) => onChange(toJson(routes.map((r, j) => (j === i ? { ...r, ...patch } : r))));
  const remove = (i: number) => onChange(toJson(routes.filter((_, j) => j !== i)));
  const move = (i: number, delta: number) => {
    const next = [...routes];
    const [item] = next.splice(i, 1);
    if (!item) return;
    next.splice(i + delta, 0, item);
    onChange(toJson(next));
  };

  return (
    <div className="space-y-2">
      {routes.map((r, i) => (
        <div key={i} className="rounded-lg border border-slate-200 bg-slate-50/60 p-2">
          <div className="flex items-center gap-1.5">
            <span className="w-4 shrink-0 text-center text-[10px] font-semibold text-slate-400" title="Keyword routing checks routes top to bottom">
              {i + 1}
            </span>
            <input
              className="am-input !py-1 font-mono text-xs font-semibold"
              value={r.name}
              onChange={(e) => update(i, { name: e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "") })}
              aria-label="Route name"
              placeholder="name"
            />
            <div className="flex shrink-0">
              <button type="button" className="rounded px-1 text-slate-400 hover:text-slate-700 disabled:opacity-30" onClick={() => move(i, -1)} disabled={i === 0} aria-label="Move up">
                ↑
              </button>
              <button type="button" className="rounded px-1 text-slate-400 hover:text-slate-700 disabled:opacity-30" onClick={() => move(i, 1)} disabled={i === routes.length - 1} aria-label="Move down">
                ↓
              </button>
              <button type="button" className="rounded px-1 text-slate-400 hover:text-red-600 disabled:opacity-30" onClick={() => remove(i)} disabled={routes.length === 1} aria-label={`Remove route ${r.name}`}>
                ×
              </button>
            </div>
          </div>
          <div className="mt-1.5 space-y-1.5 pl-5">
            <div className="flex items-center gap-1.5">
              <span className="shrink-0 text-[10px] font-medium uppercase tracking-wide text-slate-400">model</span>
              <input
                className="am-input !py-1 font-mono text-xs"
                value={r.model}
                onChange={(e) => update(i, { model: e.target.value.trim() })}
                aria-label="Model"
                placeholder="provider/model-id"
                spellCheck={false}
              />
            </div>
            <input
              className="am-input !py-1 text-xs"
              value={r.description ?? ""}
              onChange={(e) => update(i, { description: e.target.value })}
              placeholder="What kind of request goes here (read by the classifier)"
              aria-label="Description"
            />
            <KeywordsInput value={r.keywords ?? []} onCommit={(k) => update(i, { keywords: k })} />
            {openPrompt === i || r.system_prompt ? (
              <textarea
                className="am-input min-h-[48px] font-mono text-[11px]"
                value={r.system_prompt ?? ""}
                onChange={(e) => update(i, { system_prompt: e.target.value })}
                placeholder="System prompt for this route (optional)"
                aria-label="Route system prompt"
                autoFocus={openPrompt === i && !r.system_prompt}
              />
            ) : (
              <button type="button" className="text-[11px] text-accent-700 hover:underline" onClick={() => setOpenPrompt(i)}>
                + system prompt for this route
              </button>
            )}
          </div>
        </div>
      ))}
      <button
        type="button"
        className="am-btn w-full justify-center !py-1 text-xs"
        disabled={routes.length >= 12}
        onClick={() => onChange(toJson([...routes, { name: nextRouteName(routes), model: "", description: "", keywords: [] }]))}
      >
        + Add route
      </button>
    </div>
  );
}

export function RouteRefControl({
  id,
  value,
  routes,
  onChange,
}: {
  id: string;
  value: JsonValue | undefined;
  routes: JsonValue | undefined;
  onChange: (v: JsonValue) => void;
}) {
  const names = asRoutes(routes).map((r) => r.name).filter(Boolean);
  const current = typeof value === "string" ? value : "";
  return (
    <select id={id} className="am-input" value={current} onChange={(e) => onChange(e.target.value)}>
      {!names.includes(current) && <option value={current}>{current || "(choose a route)"}</option>}
      {names.map((n) => (
        <option key={n} value={n}>
          {n}
        </option>
      ))}
    </select>
  );
}

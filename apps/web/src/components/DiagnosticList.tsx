import type { Diagnostic } from "../api/types";

const SEVERITY = {
  error: "border-red-200 bg-red-50 text-red-800",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  info: "border-slate-200 bg-slate-50 text-slate-700",
} as const;

export function DiagnosticList({
  diagnostics,
  onSelect,
  compact = false,
}: {
  diagnostics: Diagnostic[];
  onSelect: (d: Diagnostic) => void;
  compact?: boolean;
}) {
  const sorted = [...diagnostics].sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "error" ? -1 : 1));
  return (
    <ul className="space-y-1.5">
      {sorted.map((d, i) => (
        <li key={`${d.code}-${d.node_id}-${d.edge_id}-${d.path}-${i}`}>
          <button
            type="button"
            onClick={() => onSelect(d)}
            className={`w-full rounded border px-2 py-1.5 text-left text-xs ${SEVERITY[d.severity]} hover:brightness-95`}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="font-mono text-[10px] font-semibold">{d.code}</span>
              {(d.node_id || d.edge_id) && !compact && (
                <span className="font-mono text-[10px] opacity-70">{d.node_id ?? d.edge_id}</span>
              )}
            </div>
            <div className="mt-0.5">{d.message}</div>
            {d.hint && <div className="mt-0.5 opacity-75">{d.hint}</div>}
          </button>
        </li>
      ))}
    </ul>
  );
}

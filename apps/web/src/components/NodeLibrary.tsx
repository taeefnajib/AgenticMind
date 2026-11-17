import { useMemo, useState } from "react";

import type { Mode, NodeManifest } from "../api/types";
import { CATEGORY_ORDER, categoryStyle } from "../lib/theme";

export const DRAG_MIME = "application/agenticmind-node";

interface Props {
  manifests: NodeManifest[];
  mode: Mode;
  onAdd: (manifest: NodeManifest) => void;
}

export function NodeLibrary({ manifests, mode, onAdd }: Props) {
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const visible = manifests.filter(
      (m) =>
        m.modes.includes(mode) &&
        (!q || m.display_name.toLowerCase().includes(q) || m.description.toLowerCase().includes(q) || m.type.includes(q)),
    );
    const byCat = new Map<string, NodeManifest[]>();
    for (const m of visible) byCat.set(m.category, [...(byCat.get(m.category) ?? []), m]);
    const order = [...CATEGORY_ORDER, ...[...byCat.keys()].filter((c) => !CATEGORY_ORDER.includes(c)).sort()];
    return order.filter((c) => byCat.has(c)).map((c) => [c, byCat.get(c)!] as const);
  }, [manifests, mode, query]);

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-200 p-3">
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Node library</div>
        <input
          className="am-input"
          placeholder="Search nodes…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="Search nodes"
        />
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {groups.length === 0 && <div className="p-3 text-sm text-slate-500">No matching nodes.</div>}
        {groups.map(([category, items]) => (
          <div key={category} className="mb-3">
            <div className="px-1 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              {categoryStyle(category).label}
            </div>
            {items.map((m) => (
              <button
                key={`${m.type}@${m.version}`}
                type="button"
                draggable
                onDragStart={(e) => {
                  e.dataTransfer.setData(DRAG_MIME, `${m.type}@${m.version}`);
                  e.dataTransfer.effectAllowed = "move";
                }}
                onClick={() => onAdd(m)}
                className="group mb-1 flex w-full cursor-grab items-start gap-2 rounded-md border border-transparent px-2 py-1.5 text-left hover:border-slate-200 hover:bg-slate-50 active:cursor-grabbing"
                title={`${m.type}@${m.version}\nClick to add, or drag onto the canvas`}
              >
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${categoryStyle(m.category).bar}`} />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-slate-800">{m.display_name}</span>
                  <span className="block truncate text-xs text-slate-500">{m.description}</span>
                </span>
              </button>
            ))}
          </div>
        ))}
      </div>
      <div className="border-t border-slate-200 p-3 text-[11px] leading-relaxed text-slate-500">
        Drag nodes onto the canvas. Connect coloured ports for data; amber squares for control flow.
      </div>
    </aside>
  );
}

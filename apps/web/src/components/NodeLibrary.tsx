import { useEffect, useMemo, useRef, useState } from "react";

import type { Mode, NodeManifest } from "../api/types";
import { CATEGORY_ORDER, categoryStyle } from "../lib/theme";

export const DRAG_MIME = "application/agenticmind-node";

/** Semantic-version comparison (pre-release suffixes sort before the release). */
export function compareVersions(a: string, b: string): number {
  const parse = (v: string) => {
    const [core = "", pre] = v.split("-", 2);
    return { nums: core.split(".").map((x) => Number(x) || 0), pre };
  };
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < 3; i++) {
    const d = (pa.nums[i] ?? 0) - (pb.nums[i] ?? 0);
    if (d !== 0) return d;
  }
  if (pa.pre && !pb.pre) return -1;
  if (!pa.pre && pb.pre) return 1;
  return (pa.pre ?? "").localeCompare(pb.pre ?? "");
}

const PERMISSION_LABEL: Record<string, string> = {
  network: "network",
  filesystem: "local files",
  database: "database",
  subprocess: "subprocess",
  credentials: "secrets",
  artifacts: "project files",
  memory: "memory",
};

/** Details shown when hovering (or focusing) a node in the library. */
export function NodeHoverCard({ manifest, top, left }: { manifest: NodeManifest; top: number; left: number }) {
  const cat = categoryStyle(manifest.category);
  const permissions = Object.entries(manifest.permissions)
    .filter(([, on]) => on)
    .map(([name]) => PERMISSION_LABEL[name] ?? name);
  const branches = manifest.branches_from_config
    ? [`one per ${manifest.branches_from_config.replace(/s$/, "")}`, ...manifest.branches]
    : manifest.branches;
  const showBranches = !(branches.length === 1 && branches[0] === "done");
  return (
    <div
      id="node-hover-card"
      role="tooltip"
      className="pointer-events-none fixed z-50 w-80 rounded-lg border border-slate-200 bg-surface p-3 text-xs shadow-lg"
      style={{ top, left }}
    >
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: cat.color }} />
        <span className="text-sm font-semibold text-slate-900">{manifest.display_name}</span>
        <span className="ml-auto text-[10px] uppercase tracking-wide text-slate-400">{cat.label}</span>
      </div>
      <p className="mt-1.5 leading-relaxed text-slate-600">{manifest.description}</p>
      {manifest.inputs.length > 0 && (
        <div className="mt-2">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Inputs</div>
          <ul className="mt-0.5 space-y-0.5">
            {manifest.inputs.map((p) => (
              <li key={p.name}>
                <span className="font-mono text-slate-700">{p.name}</span>
                <span className="text-slate-400">
                  {" "}
                  {p.type}
                  {p.cardinality === "multiple" ? "[]" : ""}
                  {p.required ? "" : " · optional"}
                </span>
                {p.description && <span className="text-slate-500"> · {p.description}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {manifest.outputs.length > 0 && (
        <div className="mt-2">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Outputs</div>
          <ul className="mt-0.5 space-y-0.5">
            {manifest.outputs.map((p) => (
              <li key={p.name}>
                <span className="font-mono text-slate-700">{p.name}</span>
                <span className="text-slate-400"> {p.type}</span>
                {p.description && <span className="text-slate-500"> · {p.description}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
      {showBranches && (
        <div className="mt-2 text-slate-500">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Branches </span>
          {branches.join(" / ")}
        </div>
      )}
      {permissions.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {permissions.map((p) => (
            <span key={p} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
              uses {p}
            </span>
          ))}
        </div>
      )}
      <div className="mt-2 border-t border-slate-100 pt-1.5 font-mono text-[10px] text-slate-400">
        {manifest.type}@{manifest.version} · click to add, or drag onto the canvas
      </div>
    </div>
  );
}

interface Props {
  manifests: NodeManifest[];
  mode: Mode;
  onAdd: (manifest: NodeManifest) => void;
}

export function NodeLibrary({ manifests, mode, onAdd }: Props) {
  const [query, setQuery] = useState("");
  const [hover, setHover] = useState<{ manifest: NodeManifest; top: number; left: number } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const showCard = (m: NodeManifest, el: HTMLElement, delay: number) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const rect = el.getBoundingClientRect();
      const aside = el.closest("aside")?.getBoundingClientRect();
      // keep the card on screen; estimated height grows with the number of ports
      const estimate = 140 + (m.inputs.length + m.outputs.length) * 18;
      const top = Math.max(8, Math.min(rect.top, window.innerHeight - estimate - 8));
      setHover({ manifest: m, top, left: (aside?.right ?? rect.right) + 8 });
    }, delay);
  };
  const hideCard = () => {
    window.clearTimeout(timer.current);
    setHover(null);
  };

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    // Older versions stay installed for existing projects but are not offered for new nodes.
    const latest = new Map<string, NodeManifest>();
    for (const m of manifests) {
      const cur = latest.get(m.type);
      if (!cur || compareVersions(m.version, cur.version) > 0) latest.set(m.type, m);
    }
    const visible = [...latest.values()].filter(
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
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-slate-200 bg-surface">
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
      <div className="flex-1 overflow-y-auto p-2" onScroll={hideCard}>
        {manifests.length === 0 && (
          <div className="p-3 text-sm text-slate-500">Nodes appear once the API is connected.</div>
        )}
        {manifests.length > 0 && groups.length === 0 && (
          <div className="p-3 text-sm text-slate-500">No matching nodes.</div>
        )}
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
                  hideCard();
                  e.dataTransfer.setData(DRAG_MIME, `${m.type}@${m.version}`);
                  e.dataTransfer.effectAllowed = "move";
                }}
                onClick={() => onAdd(m)}
                className="group mb-1 flex w-full cursor-grab items-start gap-2 rounded-md border border-transparent px-2 py-1.5 text-left hover:border-slate-200 hover:bg-slate-50 active:cursor-grabbing"
                onMouseEnter={(e) => showCard(m, e.currentTarget, 350)}
                onMouseLeave={hideCard}
                onFocus={(e) => showCard(m, e.currentTarget, 0)}
                onBlur={hideCard}
                aria-describedby={hover?.manifest === m ? "node-hover-card" : undefined}
              >
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ backgroundColor: categoryStyle(m.category).color }} />
                <span className="min-w-0">
                  <span className="block text-sm font-medium text-slate-800">{m.display_name}</span>
                  <span className="block truncate text-xs text-slate-500">{m.description}</span>
                </span>
              </button>
            ))}
          </div>
        ))}
      </div>
      {hover && <NodeHoverCard manifest={hover.manifest} top={hover.top} left={hover.left} />}
      <div className="border-t border-slate-200 p-3 text-[11px] leading-relaxed text-slate-500">
        Drag nodes onto the canvas. Connect coloured ports for data; amber squares for control flow.
      </div>
    </aside>
  );
}

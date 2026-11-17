import { useEffect, useState } from "react";

import type { TemplateInfo } from "../api/types";

interface Props {
  open: boolean;
  templates: TemplateInfo[];
  onCancel: () => void;
  onCreate: (name: string, template: string | null) => void;
  onImport: (file: File) => void;
}

export function NewProjectDialog({ open, templates, onCancel, onCreate, onImport }: Props) {
  const [name, setName] = useState("Untitled project");
  const [template, setTemplate] = useState<string | null>("hello-pipeline");

  useEffect(() => {
    if (open) {
      setName("Untitled project");
      setTemplate(templates.some((t) => t.name === "hello-pipeline") ? "hello-pipeline" : null);
    }
  }, [open, templates]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-labelledby="np-title">
      <div className="w-full max-w-lg rounded-xl bg-surface p-5 shadow-xl">
        <h2 id="np-title" className="text-lg font-semibold text-slate-900">
          New project
        </h2>
        <label htmlFor="np-name" className="mb-1 mt-4 block text-xs font-medium text-slate-700">
          Name
        </label>
        <input id="np-name" className="am-input" value={name} autoFocus onChange={(e) => setName(e.target.value)} />
        <div className="mb-1 mt-4 text-xs font-medium text-slate-700">Start from</div>
        <div className="space-y-1.5">
          <TemplateOption selected={template === null} onSelect={() => setTemplate(null)} title="Blank canvas" description="An empty graph." />
          {templates.map((t) => (
            <TemplateOption
              key={t.name}
              selected={template === t.name}
              onSelect={() => setTemplate(t.name)}
              title={t.title}
              description={`${t.description} (${t.node_count} nodes)`}
            />
          ))}
        </div>
        <div className="mt-5 flex items-center justify-between">
          <label className="am-btn cursor-pointer">
            Import IR…
            <input
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) onImport(f);
                e.target.value = "";
              }}
            />
          </label>
          <div className="flex gap-2">
            <button type="button" className="am-btn" onClick={onCancel}>
              Cancel
            </button>
            <button type="button" className="am-btn-primary" disabled={!name.trim()} onClick={() => onCreate(name.trim(), template)}>
              Create
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function TemplateOption({ selected, onSelect, title, description }: { selected: boolean; onSelect: () => void; title: string; description: string }) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={`w-full rounded-lg border px-3 py-2 text-left ${selected ? "border-fuchsia-500 bg-fuchsia-50" : "border-slate-200 hover:bg-slate-50"}`}
    >
      <div className="text-sm font-medium text-slate-900">{title}</div>
      <div className="text-xs text-slate-500">{description}</div>
    </button>
  );
}

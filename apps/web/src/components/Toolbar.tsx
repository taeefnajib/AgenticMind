import type { Mode, ProjectSummary } from "../api/types";
import { MODE_LABEL } from "../lib/theme";

interface Props {
  projects: ProjectSummary[];
  projectId: string | null;
  revision: number | null;
  dirty: boolean;
  busy: boolean;
  running: boolean;
  valid: boolean | null;
  mode: Mode;
  message: { text: string; tone: "info" | "error" | "success" } | null;
  onSelectProject: (id: string) => void;
  onNew: () => void;
  onSave: () => void;
  onValidate: () => void;
  onRun: () => void;
  onCancel: () => void;
  onExport: () => void;
  onMode: (mode: Mode) => void;
}

const TONE = { info: "text-slate-600", error: "text-red-600", success: "text-emerald-700" } as const;

export function Toolbar(p: Props) {
  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-slate-200 bg-white px-3">
      <div className="flex items-center gap-2 pr-2">
        <div className="flex h-7 w-7 items-center justify-center rounded-md bg-slate-900 text-xs font-bold text-white">AM</div>
        <span className="hidden text-sm font-semibold text-slate-900 md:inline">AgenticMind</span>
      </div>

      <select
        className="am-input !w-56 shrink-0"
        value={p.projectId ?? ""}
        onChange={(e) => p.onSelectProject(e.target.value)}
        aria-label="Project"
      >
        {p.projects.map((proj) => (
          <option key={proj.id} value={proj.id}>
            {proj.name}
          </option>
        ))}
      </select>
      <button type="button" className="am-btn" onClick={p.onNew}>
        New
      </button>
      {p.revision !== null && (
        <span className="hidden whitespace-nowrap text-xs text-slate-500 lg:inline" title="Saved revision">
          rev {p.revision}
          {p.dirty && <span className="ml-1 text-amber-600">● unsaved</span>}
        </span>
      )}

      <div className="mx-2 h-6 w-px bg-slate-200" />

      <button type="button" className="am-btn" onClick={p.onSave} disabled={!p.projectId || p.busy} title="Save (Ctrl+S)">
        Save
      </button>
      <button type="button" className="am-btn" onClick={p.onValidate} disabled={!p.projectId || p.busy}>
        Validate
        {p.valid !== null && <span className={`h-2 w-2 rounded-full ${p.valid ? "bg-emerald-500" : "bg-red-500"}`} />}
      </button>
      {p.running ? (
        <button type="button" className="am-btn text-red-600" onClick={p.onCancel}>
          Cancel run
        </button>
      ) : (
        <button type="button" className="am-btn-primary" onClick={p.onRun} disabled={!p.projectId || p.busy}>
          ▶ Run
        </button>
      )}
      <button type="button" className="am-btn" onClick={p.onExport} disabled={!p.projectId || p.busy} title="Download canonical IR">
        Export IR
      </button>

      <div className={`ml-2 min-w-0 flex-1 truncate text-xs ${p.message ? TONE[p.message.tone] : ""}`} role="status">
        {p.message?.text}
      </div>

      <div className="flex rounded-md border border-slate-300 p-0.5" role="radiogroup" aria-label="Experience mode">
        {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={p.mode === m}
            onClick={() => p.onMode(m)}
            className={`whitespace-nowrap rounded px-2 py-0.5 text-xs font-medium ${p.mode === m ? "bg-slate-900 text-white" : "text-slate-600 hover:bg-slate-100"}`}
          >
            {MODE_LABEL[m]}
          </button>
        ))}
      </div>
    </header>
  );
}

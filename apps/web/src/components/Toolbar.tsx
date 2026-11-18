import type { Mode, ProjectSummary } from "../api/types";
import { MODE_LABEL } from "../lib/theme";
import { NEXT_THEME, type ThemePreference } from "../lib/useTheme";

export interface RunTargetOption {
  id: string;
  label: string;
}

interface Props {
  runTargets: RunTargetOption[];
  runTarget: string;
  onRunTarget: (id: string) => void;
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
  onExportApp: () => void;
  onMode: (mode: Mode) => void;
  theme: ThemePreference;
  onTheme: (theme: ThemePreference) => void;
}

const TONE = { info: "text-slate-600", error: "text-red-600", success: "text-emerald-700" } as const;

export function Toolbar(p: Props) {
  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-slate-200 bg-surface px-3">
      <div className="flex items-center gap-2 pr-2">
        <img src="/logo.svg" alt="" className="h-6 w-auto" />
        <span className="hidden text-[15px] font-semibold tracking-tight text-slate-900 md:inline">AgenticMind</span>
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
      {p.runTargets.length > 1 && !p.running && (
        <select
          className="am-input !w-44 shrink-0"
          value={p.runTarget}
          onChange={(e) => p.onRunTarget(e.target.value)}
          aria-label="What to run"
          title="Run the whole graph, or only what one output needs"
        >
          <option value="">Run everything</option>
          {p.runTargets.map((t) => (
            <option key={t.id} value={t.id}>
              Run → {t.label}
            </option>
          ))}
        </select>
      )}
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
      <button
        type="button"
        className="am-btn"
        onClick={p.onExportApp}
        disabled={!p.projectId || p.busy}
        title="Generate a standalone FastAPI project (API, tests, Docker) from this graph"
      >
        Export app
      </button>

      <div className={`ml-2 min-w-0 flex-1 truncate text-xs ${p.message ? TONE[p.message.tone] : ""}`} role="status">
        {p.message?.text}
      </div>

      <div className="flex rounded-md border border-slate-200 p-0.5" role="radiogroup" aria-label="Experience mode">
        {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={p.mode === m}
            onClick={() => p.onMode(m)}
            className={`whitespace-nowrap rounded px-2 py-0.5 text-xs font-medium ${p.mode === m ? "bg-accent-100 text-accent-700" : "text-slate-500 hover:text-slate-800"}`}
          >
            {MODE_LABEL[m]}
          </button>
        ))}
      </div>
      <ThemeToggle theme={p.theme} onTheme={p.onTheme} />
    </header>
  );
}

const THEME_LABEL: Record<ThemePreference, string> = { light: "Light", dark: "Dark", system: "System" };

function ThemeToggle({ theme, onTheme }: { theme: ThemePreference; onTheme: (t: ThemePreference) => void }) {
  return (
    <button
      type="button"
      className="am-btn !px-2"
      onClick={() => onTheme(NEXT_THEME[theme])}
      title={`Theme: ${THEME_LABEL[theme]} (click for ${THEME_LABEL[NEXT_THEME[theme]]})`}
      aria-label={`Theme: ${THEME_LABEL[theme]}. Switch to ${THEME_LABEL[NEXT_THEME[theme]]}`}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {theme === "light" && (
          <>
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
          </>
        )}
        {theme === "dark" && <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />}
        {theme === "system" && (
          <>
            <rect x="3" y="4" width="18" height="12" rx="2" />
            <path d="M8 20h8M12 16v4" />
          </>
        )}
      </svg>
      <span className="hidden text-xs xl:inline">{THEME_LABEL[theme]}</span>
    </button>
  );
}

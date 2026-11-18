import { useRef, useState } from "react";

import { api } from "../api/client";
import type { ArtifactInfo } from "../api/types";
import { useWorkbench } from "../lib/workbench";

export const ACCEPT = ".pdf,.docx,.txt,.md,.markdown,.html,.htm,.csv,.json,.sqlite,.sqlite3,.db";

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function fileIcon(name: string): string {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  return { pdf: "PDF", docx: "DOC", md: "MD", markdown: "MD", html: "HTM", htm: "HTM", csv: "CSV", json: "JSN", txt: "TXT", sqlite: "DB", sqlite3: "DB", db: "DB" }[ext] ?? "FILE";
}

/** Upload button + drop zone shared by the project Files panel and the file picker field. */
export function UploadZone({ onUploaded, compact = false }: { onUploaded?: (files: ArtifactInfo[]) => void; compact?: boolean }) {
  const { files } = useWorkbench();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [over, setOver] = useState(false);

  const handle = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      onUploaded?.(await files.upload(list));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          void handle(e.dataTransfer.files);
        }}
        className={`flex items-center justify-center rounded-md border border-dashed text-xs ${
          over ? "border-accent-500 bg-accent-50" : "border-slate-300 bg-slate-50"
        } ${compact ? "px-2 py-2" : "px-3 py-4"}`}
      >
        <button type="button" className="am-btn" disabled={busy || !files.projectId} onClick={() => input.current?.click()}>
          {busy ? "Uploading…" : "Upload files"}
        </button>
        {!compact && <span className="ml-2 text-slate-500">or drop them here</span>}
        <input
          ref={input}
          type="file"
          multiple
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => {
            void handle(e.target.files);
            e.target.value = "";
          }}
        />
      </div>
      {!compact && <p className="mt-1 text-[11px] text-slate-400">PDF, DOCX, TXT, MD, HTML, CSV, JSON, SQLite</p>}
      {error && <p className="mt-1 text-[11px] text-red-600">{error}</p>}
    </div>
  );
}

/** Project-level file manager (shown in the Inspector when nothing is selected). */
export function FilesPanel() {
  const { files } = useWorkbench();
  return (
    <div className="space-y-2">
      <UploadZone />
      {files.files.length === 0 ? (
        <p className="text-xs text-slate-500">No files yet. Uploaded files can be used by Document Loader nodes.</p>
      ) : (
        <ul className="divide-y divide-slate-100 rounded-md border border-slate-200">
          {files.files.map((f) => (
            <li key={f.id} className="flex items-center gap-2 px-2 py-1.5">
              <span className="w-8 shrink-0 rounded bg-slate-100 py-0.5 text-center font-mono text-[9px] font-semibold text-slate-600">
                {fileIcon(f.filename)}
              </span>
              <span className="min-w-0 flex-1">
                <a
                  className="block truncate text-xs font-medium text-slate-800 hover:underline"
                  href={files.projectId ? api.fileUrl(files.projectId, f.id) : undefined}
                  title={`${f.filename}\n${f.id}\nsha256 ${f.sha256}`}
                >
                  {f.filename}
                </a>
                <span className="text-[10px] text-slate-500">{formatBytes(f.size)}</span>
              </span>
              <button
                type="button"
                className="rounded px-1.5 text-xs text-slate-400 hover:bg-red-50 hover:text-red-600"
                title={`Delete ${f.filename}`}
                aria-label={`Delete ${f.filename}`}
                onClick={() => void files.remove(f.id)}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Config field for selecting project files (``x-ui.widget = "artifacts"``). */
export function ArtifactsControl({ value, onChange }: { value: unknown; onChange: (ids: string[]) => void }) {
  const { files } = useWorkbench();
  const selected = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  const known = new Set(files.files.map((f) => f.id));
  const missing = selected.filter((id) => !known.has(id));
  const toggle = (id: string) => onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);

  return (
    <div className="space-y-2">
      {files.files.length > 0 && (
        <ul className="max-h-48 overflow-y-auto rounded-md border border-slate-200">
          {files.files.map((f) => (
            <li key={f.id}>
              <label className="flex cursor-pointer items-center gap-2 px-2 py-1 text-xs hover:bg-slate-50">
                <input type="checkbox" className="accent-accent-600" checked={selected.includes(f.id)} onChange={() => toggle(f.id)} />
                <span className="min-w-0 flex-1 truncate">{f.filename}</span>
                <span className="text-[10px] text-slate-400">{formatBytes(f.size)}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {missing.length > 0 && (
        <p className="text-[11px] text-amber-700">
          {missing.length} selected file{missing.length > 1 ? "s are" : " is"} missing from this project.{" "}
          <button type="button" className="underline" onClick={() => onChange(selected.filter((id) => known.has(id)))}>
            Remove
          </button>
        </p>
      )}
      <UploadZone compact onUploaded={(added) => onChange([...selected, ...added.map((a) => a.id)])} />
      <p className="text-[11px] text-slate-500">
        {selected.length} of {files.files.length} file{files.files.length === 1 ? "" : "s"} selected
      </p>
    </div>
  );
}

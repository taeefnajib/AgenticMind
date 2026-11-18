import { useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import type { AppPreview } from "../api/types";
import { formatBytes } from "./Files";

interface Props {
  open: boolean;
  projectId: string | null;
  /** Saves pending edits first; resolves false if saving failed. */
  ensureSaved: () => Promise<boolean>;
  onClose: () => void;
  notify: (text: string, tone?: "info" | "error" | "success") => void;
}

function List({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <span className="text-slate-400">{empty}</span>;
  return (
    <ul className="list-disc space-y-0.5 pl-4">
      {items.map((i) => (
        <li key={i}>{i}</li>
      ))}
    </ul>
  );
}

/** "Export app": preview what the compiler generates, then download the standalone project (ADR-0016). */
export function ExportAppDialog({ open, projectId, ensureSaved, onClose, notify }: Props) {
  const [preview, setPreview] = useState<AppPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const save = useRef(ensureSaved);
  save.current = ensureSaved;

  useEffect(() => {
    if (!open || !projectId) return;
    let cancelled = false;
    setPreview(null);
    setError(null);
    setProblems([]);
    void (async () => {
      if (!(await save.current())) {
        if (!cancelled) setError("Save the project first.");
        return;
      }
      try {
        const p = await api.appPreview(projectId);
        if (!cancelled) setPreview(p);
      } catch (err) {
        if (cancelled) return;
        const detail = err instanceof ApiError ? (err.detail as { message?: string; diagnostics?: { message: string; path?: string }[] }) : null;
        setError(detail?.message ?? (err as Error).message);
        setProblems((detail?.diagnostics ?? []).map((d) => (d.path ? `${d.path}: ${d.message}` : d.message)));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, projectId]);

  if (!open) return null;

  const download = async () => {
    if (!projectId) return;
    setBusy(true);
    try {
      const { blob, filename } = await api.exportApp(projectId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
      notify(`Exported ${filename}`, "success");
      onClose();
    } catch (err) {
      notify((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const req = preview?.requirements;
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/30 p-4" role="dialog" aria-modal="true" aria-labelledby="export-app-title">
      <div className="flex max-h-[calc(100vh-2rem)] w-full max-w-2xl flex-col rounded-xl bg-surface p-5 shadow-xl">
        <h2 id="export-app-title" className="text-base font-semibold text-slate-900">
          Export as app
        </h2>
        <p className="mt-1 text-xs text-slate-500">
          A standalone FastAPI project: the graph, its API, tests, Dockerfile and the AgenticMind runtime it needs. It runs
          without AgenticMind and contains no secret values.
        </p>

        <div className="mt-3 min-h-0 flex-1 space-y-4 overflow-y-auto pr-1 text-xs">
          {!preview && !error && <p className="text-slate-400">Compiling…</p>}
          {error && (
            <div className="rounded border border-red-200 bg-red-50 p-2 text-red-800">
              <div className="font-medium">{error}</div>
              {problems.length > 0 && <List items={problems} empty="" />}
            </div>
          )}
          {preview && req && (
            <>
              <section>
                <h3 className="mb-1 font-semibold text-slate-700">Endpoints</h3>
                <table className="w-full border-collapse">
                  <thead>
                    <tr className="text-left text-[10px] uppercase tracking-wide text-slate-400">
                      <th className="py-1 pr-2 font-medium">Route</th>
                      <th className="py-1 pr-2 font-medium">Request</th>
                      <th className="py-1 pr-2 font-medium">Response</th>
                      <th className="py-1 font-medium">Auth</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.endpoints.map((ep) => (
                      <tr key={ep.name} className="border-t border-slate-100 align-top">
                        <td className="py-1 pr-2 font-mono text-slate-800">POST {ep.path}</td>
                        <td className="py-1 pr-2 text-slate-600">
                          {ep.request.length
                            ? ep.request.map((f) => `${f.key}${f.required ? "" : "?"}: ${f.type}`).join(", ")
                            : "—"}
                        </td>
                        <td className="py-1 pr-2 text-slate-600">{ep.response.map((r) => r.key).join(", ")}</td>
                        <td className="py-1 text-slate-600">{ep.auth === "api_key" ? "API key" : "none"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              <section className="grid gap-3 sm:grid-cols-2">
                <div>
                  <h3 className="mb-1 font-semibold text-slate-700">Environment variables</h3>
                  <List items={["APP_API_KEYS (accepted X-API-Key values)", ...req.secrets.map((s) => `${s} (secret)`)]} empty="none" />
                </div>
                <div>
                  <h3 className="mb-1 font-semibold text-slate-700">Needs</h3>
                  <List
                    items={[
                      ...(req.network ? ["Network access (model providers or web tools)"] : []),
                      ...req.local_models,
                      ...req.services,
                      ...req.bundled_files.map((f) => `Bundled file: ${f}`),
                    ]}
                    empty="Nothing beyond Python 3.11"
                  />
                </div>
              </section>

              {preview.warnings.length > 0 && (
                <section className="rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">
                  <h3 className="mb-1 font-semibold">Notes</h3>
                  <List items={preview.warnings} empty="" />
                </section>
              )}

              <section className="text-slate-500">
                {preview.files} files · {formatBytes(preview.bytes)} · vendored{" "}
                {preview.packages.map((p) => p.name.replace("agenticmind-", "")).join(", ")} · formatter{" "}
                {preview.checks.formatter.replace(/^ruff /, "ruff ")}, lint {preview.checks.lint === "ok" ? "ok" : "findings"}
              </section>
            </>
          )}
        </div>

        <div className="mt-4 flex items-center justify-end gap-2">
          <button type="button" className="am-btn" onClick={onClose}>
            Close
          </button>
          <button type="button" className="am-btn-primary" disabled={!preview || busy} onClick={() => void download()}>
            {busy ? "Preparing…" : `Download ${preview?.name ?? "app"}.zip`}
          </button>
        </div>
      </div>
    </div>
  );
}

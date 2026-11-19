import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import type { NodePackage, NodePackages as NodePackagesData } from "../api/types";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Called after anything that changes which nodes exist (reload the node library). */
  onChanged: () => void;
  notify: (text: string, tone?: "info" | "error" | "success") => void;
}

/** What each permission lets a node do, in the words shown when approving a package. */
export const PERMISSION_EXPLAINED: Record<string, string> = {
  network: "Make network requests (e.g. call web APIs)",
  credentials: "Read the secrets referenced in its own node settings",
  filesystem: "Keep files in a storage folder of its own",
  subprocess: "Start other programs inside its worker",
  database: "Connect to databases it is configured for",
  artifacts: "Read files uploaded to your projects (not available to packages yet)",
  memory: "Read and write conversation memory (not available to packages yet)",
};

const STATUS_STYLE = {
  pending: "bg-amber-100 text-amber-800",
  approved: "bg-emerald-100 text-emerald-800",
  disabled: "bg-slate-100 text-slate-600",
} as const;
const STATUS_LABEL = { pending: "Needs approval", approved: "Approved", disabled: "Disabled" } as const;

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { message?: string } | string | undefined;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
  }
  return (err as Error).message;
}

function PackageCard({ pkg, onAction }: { pkg: NodePackage; onAction: () => void }) {
  const [reviewing, setReviewing] = useState(false);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [trusted, setTrusted] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allChecked = pkg.requested.every((p) => checked[p]) && trusted;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setReviewing(false);
      onAction();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const download = async () => {
    const { blob, filename } = await api.nodePackageWheel(pkg.name);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <article className="rounded-md border border-slate-200 p-3">
      <header className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-semibold text-slate-900">{pkg.name}</span>
        <span className="text-slate-500">{pkg.version}</span>
        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLE[pkg.status]}`}>{STATUS_LABEL[pkg.status]}</span>
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">{pkg.trust} · runs isolated</span>
        <span className="ml-auto font-mono text-[10px] text-slate-400" title={pkg.sha256}>
          sha256 {pkg.sha256.slice(0, 12)}…
        </span>
      </header>
      {pkg.summary && <p className="mt-1 text-slate-600">{pkg.summary}</p>}
      <ul className="mt-2 space-y-1">
        {pkg.nodes.map((n) => (
          <li key={`${n.type}@${n.version}`}>
            <span className="font-medium text-slate-800">{n.display_name}</span>{" "}
            <span className="font-mono text-[11px] text-slate-400">
              {n.type}@{n.version}
            </span>
            <div className="text-slate-500">{n.description}</div>
          </li>
        ))}
      </ul>
      <div className="mt-2 text-slate-600">
        Permissions requested:{" "}
        {pkg.requested.length === 0 ? <span className="text-emerald-700">none (it only computes)</span> : pkg.requested.join(", ")}
      </div>
      {pkg.unsupported.length > 0 && (
        <div className="mt-2 rounded border border-red-200 bg-red-50 p-2 text-red-800">
          Needs {pkg.unsupported.join(", ")}, which packages cannot have yet: they run in an isolated worker without access
          to project files or memory. It cannot be approved.
        </div>
      )}

      {reviewing && (
        <div className="mt-3 space-y-1.5 rounded border border-accent-200 bg-accent-50 p-2">
          <div className="font-semibold text-slate-800">Approve {pkg.name} {pkg.version}</div>
          {pkg.requested.map((p) => (
            <label key={p} className="flex items-start gap-2">
              <input type="checkbox" checked={!!checked[p]} onChange={(e) => setChecked({ ...checked, [p]: e.target.checked })} />
              <span>
                <span className="font-medium">{p}</span>: {PERMISSION_EXPLAINED[p] ?? p}
              </span>
            </label>
          ))}
          <label className="flex items-start gap-2">
            <input type="checkbox" checked={trusted} onChange={(e) => setTrusted(e.target.checked)} />
            <span>
              I trust this package to run in graphs. Each node runs in its own worker process with only the permissions
              above, no host environment variables, and only the secrets referenced in its own settings.
            </span>
          </label>
          <div className="flex gap-2 pt-1">
            <button
              type="button"
              className="am-btn-primary"
              disabled={!allChecked || busy}
              onClick={() => void act(() => api.approveNodePackage(pkg.name, pkg.requested, pkg.sha256))}
            >
              Approve
            </button>
            <button type="button" className="am-btn" onClick={() => setReviewing(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {error && <div className="mt-2 text-red-700">{error}</div>}

      <footer className="mt-3 flex flex-wrap gap-1.5">
        {pkg.status === "pending" && pkg.unsupported.length === 0 && !reviewing && (
          <button type="button" className="am-btn-primary" onClick={() => setReviewing(true)}>
            Review &amp; approve
          </button>
        )}
        {pkg.status !== "disabled" ? (
          <button type="button" className="am-btn" disabled={busy} onClick={() => void act(() => api.setNodePackageEnabled(pkg.name, false))}>
            Disable
          </button>
        ) : (
          <button type="button" className="am-btn" disabled={busy} onClick={() => void act(() => api.setNodePackageEnabled(pkg.name, true))}>
            Enable
          </button>
        )}
        <button type="button" className="am-btn" onClick={() => void download().catch((err) => setError(errorText(err)))}>
          Download .whl
        </button>
        {confirmRemove ? (
          <>
            <span className="self-center text-slate-600">Remove the package and its nodes?</span>
            <button type="button" className="am-btn" disabled={busy} onClick={() => void act(() => api.removeNodePackage(pkg.name))}>
              Yes, remove
            </button>
            <button type="button" className="am-btn" onClick={() => setConfirmRemove(false)}>
              Keep
            </button>
          </>
        ) : (
          <button type="button" className="am-btn" onClick={() => setConfirmRemove(true)}>
            Remove
          </button>
        )}
      </footer>
    </article>
  );
}

/** Node packages (ADR-0021): install third-party nodes from a wheel, review and approve their permissions. */
export function NodePackages({ open, onClose, onChanged, notify }: Props) {
  const [data, setData] = useState<NodePackagesData | null>(null);
  const [trust, setTrust] = useState("community");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      setData(await api.nodePackages());
    } catch (err) {
      setError(errorText(err));
    }
  }, []);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  if (!open) return null;

  const install = async (file: File) => {
    setUploading(true);
    setError(null);
    try {
      const pkg = await api.installNodePackage(file, trust);
      notify(`Installed ${pkg.name} ${pkg.version}: review its permissions to use it`, "success");
      await refresh();
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setUploading(false);
      if (input.current) input.current.value = "";
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/30 p-4" role="dialog" aria-modal="true" aria-labelledby="pkg-title">
      <div className="flex max-h-[calc(100vh-2rem)] w-full max-w-3xl flex-col rounded-xl bg-surface p-5 shadow-xl">
        <h2 id="pkg-title" className="text-base font-semibold text-slate-900">
          Node packages
        </h2>
        <p className="mt-1 text-xs text-slate-500">
          Add nodes made by others (or by you with <span className="font-mono">agenticmind-node</span>). A package is a
          Python wheel; it is installed in its own folder, never imported into AgenticMind, and its nodes run in isolated
          worker processes once you approve the permissions it asks for.
        </p>

        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <input
            ref={input}
            type="file"
            accept=".whl"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void install(f);
            }}
          />
          <button type="button" className="am-btn-primary" disabled={uploading} onClick={() => input.current?.click()}>
            {uploading ? "Installing…" : "Install from .whl…"}
          </button>
          <label className="flex items-center gap-1 text-slate-600">
            Source
            <select className="am-input w-auto py-1 text-xs" value={trust} onChange={(e) => setTrust(e.target.value)}>
              <option value="community">community (third party)</option>
              <option value="private">private (your organisation)</option>
            </select>
          </label>
        </div>
        {error && <div className="mt-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-800">{error}</div>}

        <div className="mt-3 min-h-0 flex-1 space-y-2 overflow-y-auto pr-1 text-xs">
          {data && data.packages.length === 0 && <p className="text-slate-400">No packages installed.</p>}
          {data?.packages.map((p) => (
            <PackageCard
              key={`${p.name}@${p.version}`}
              pkg={p}
              onAction={() => {
                void refresh();
                onChanged();
              }}
            />
          ))}
          {data && data.skipped_in_environment.length > 0 && (
            <p className="rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">
              Found in the server's Python environment but not loaded: {data.skipped_in_environment.join(", ")}. Packages
              installed that way would run inside AgenticMind; install their wheels here instead.
            </p>
          )}
        </div>
        <div className="mt-4 flex justify-end">
          <button type="button" className="am-btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

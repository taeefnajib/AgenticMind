import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import type { DeployPlan, DeployTargets, Deployment } from "../api/types";
import {
  MODE_LABEL,
  STATUS_LABEL,
  bySeverity,
  canRollBackTo,
  formatEnv,
  isActive,
  parseEnv,
  statusTone,
} from "../lib/deployment";
import { formatBytes } from "./Files";

interface Props {
  open: boolean;
  projectId: string | null;
  ensureSaved: () => Promise<boolean>;
  onClose: () => void;
  notify: (text: string, tone?: "info" | "error" | "success") => void;
}

const SEVERITY_STYLE = {
  blocker: "border-red-200 bg-red-50 text-red-900",
  warning: "border-amber-200 bg-amber-50 text-amber-900",
  info: "border-slate-200 bg-slate-50 text-slate-700",
} as const;
const SEVERITY_LABEL = { blocker: "Blocker", warning: "Warning", info: "Note" } as const;
const TONE_STYLE = {
  ok: "bg-emerald-100 text-emerald-800",
  busy: "bg-sky-100 text-sky-800",
  bad: "bg-red-100 text-red-800",
  idle: "bg-slate-100 text-slate-600",
} as const;

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { message?: string } | string | undefined;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
  }
  return (err as Error).message;
}

async function save(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Deployment Center (ADR-0020): pick a target and profile, review the plan, confirm, deploy, roll back. */
export function DeploymentCenter({ open, projectId, ensureSaved, onClose, notify }: Props) {
  const [tab, setTab] = useState<"new" | "history">("new");
  const [targets, setTargets] = useState<DeployTargets | null>(null);
  const [target, setTarget] = useState("local");
  const [profile, setProfile] = useState("development");
  const [envText, setEnvText] = useState("");
  const [image, setImage] = useState("");
  const [revision, setRevision] = useState<number | null>(null);
  const [rollbackOf, setRollbackOf] = useState<string | null>(null);
  const [plan, setPlan] = useState<DeployPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [planning, setPlanning] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [viewFile, setViewFile] = useState<string | null>(null);
  const [history, setHistory] = useState<Deployment[]>([]);
  const [selected, setSelected] = useState<Deployment | null>(null);
  const [urlDraft, setUrlDraft] = useState("");
  const saveRef = useRef(ensureSaved);
  saveRef.current = ensureSaved;

  const { env, errors: envErrors } = parseEnv(envText);
  const envKey = JSON.stringify(env);

  const refreshHistory = useCallback(async () => {
    if (!projectId) return;
    try {
      setHistory(await api.listDeployments(projectId));
    } catch {
      /* shown on next action */
    }
  }, [projectId]);

  useEffect(() => {
    if (!open || !projectId) return;
    void api.deployTargets().then(setTargets, (err) => setPlanError(errorText(err)));
    void refreshHistory();
  }, [open, projectId, refreshHistory]);

  // (re)plan whenever the inputs change; a confirmation always applies to the plan on screen
  useEffect(() => {
    if (!open || !projectId || envErrors.length) return;
    let cancelled = false;
    setConfirmed(false);
    setPlanning(true);
    setPlanError(null);
    const timer = setTimeout(() => {
      void (async () => {
        if (!(await saveRef.current())) {
          if (!cancelled) setPlanError("Save the project first.");
          return;
        }
        try {
          const p = await api.deployPlan(projectId, { revision, target, profile, env, image });
          if (!cancelled) {
            setPlan(p);
            setViewFile((f) => (f && p.files && f in p.files ? f : null));
          }
        } catch (err) {
          if (!cancelled) {
            setPlan(null);
            setPlanError(errorText(err));
          }
        } finally {
          if (!cancelled) setPlanning(false);
        }
      })();
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [open, projectId, target, profile, envKey, image, revision, envErrors.length]);

  // follow deployments that are still in progress
  useEffect(() => {
    if (!open || !isActive(history)) return;
    const t = setInterval(() => void refreshHistory(), 2000);
    return () => clearInterval(t);
  }, [open, history, refreshHistory]);

  useEffect(() => {
    if (!selected) return;
    const fresh = history.find((d) => d.id === selected.id);
    if (fresh && fresh.status !== selected.status) void api.getDeployment(selected.id).then(setSelected);
  }, [history, selected]);

  if (!open) return null;
  const spec = targets?.targets.find((t) => t.id === target);

  const deploy = async () => {
    if (!projectId || !plan) return;
    setBusy(true);
    try {
      const dep = await api.deploy(projectId, {
        revision,
        target,
        profile,
        env,
        image,
        plan_hash: plan.plan_hash,
        confirm: confirmed,
        rollback_of: rollbackOf,
      });
      notify(
        dep.mode === "direct" ? `Deploying revision ${dep.revision} to ${dep.target_name}…` : "Deployment files are ready",
        "success",
      );
      if (dep.mode !== "direct") {
        const { blob, filename } = await api.deploymentBundle(dep.id);
        await save(blob, filename);
      }
      setRollbackOf(null);
      setRevision(null);
      await refreshHistory();
      setSelected(await api.getDeployment(dep.id));
      setTab("history");
    } catch (err) {
      notify(errorText(err), "error");
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<Deployment>, done: string) => {
    try {
      const dep = await fn();
      notify(done, "success");
      await refreshHistory();
      setSelected(await api.getDeployment(dep.id));
    } catch (err) {
      notify(errorText(err), "error");
    }
  };

  const rollBack = (dep: Deployment) => {
    setTarget(dep.target);
    setProfile(dep.profile);
    setEnvText(formatEnv(dep.env));
    setImage(dep.image);
    setRevision(dep.revision);
    setRollbackOf(dep.id);
    setTab("new");
  };

  const actionLabel = !plan
    ? "Deploy"
    : plan.mode === "direct"
      ? rollbackOf
        ? `Roll back to revision ${plan.revision}`
        : `Deploy revision ${plan.revision}`
      : plan.mode === "guided"
        ? "Generate & download files"
        : "Download plan";

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-900/30 p-4" role="dialog" aria-modal="true" aria-labelledby="deploy-title">
      <div className="flex h-[calc(100vh-2rem)] w-full max-w-5xl flex-col rounded-xl bg-surface p-5 shadow-xl">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="deploy-title" className="text-base font-semibold text-slate-900">
              Deployment Center
            </h2>
            <p className="mt-1 text-xs text-slate-500">
              Checks what this graph needs against each target before anything runs. Nothing is deployed until you
              confirm the plan; secrets are passed by name and never written into files.
            </p>
          </div>
          <div className="flex rounded-md border border-slate-200 p-0.5 text-xs" role="tablist">
            {(["new", "history"] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                className={`whitespace-nowrap rounded-md px-3 py-1 ${tab === t ? "bg-accent-100 font-medium text-accent-800" : "text-slate-600 hover:bg-slate-100"}`}
                onClick={() => setTab(t)}
              >
                {t === "new" ? "New deployment" : `History (${history.length})`}
              </button>
            ))}
          </div>
        </div>

        {tab === "new" ? (
          <div className="mt-4 grid min-h-0 flex-1 gap-4 text-xs md:grid-cols-[17rem_1fr]">
            {/* ---- choices ---- */}
            <div className="min-h-0 space-y-3 overflow-y-auto pr-1">
              {rollbackOf && (
                <div className="rounded border border-sky-200 bg-sky-50 p-2 text-sky-900">
                  Rolling back to revision {revision}.{" "}
                  <button type="button" className="underline" onClick={() => (setRollbackOf(null), setRevision(null))}>
                    Cancel
                  </button>
                </div>
              )}
              <fieldset>
                <legend className="mb-1 font-semibold text-slate-700">Target</legend>
                <div className="space-y-1.5">
                  {targets?.targets.map((t) => (
                    <label
                      key={t.id}
                      className={`block cursor-pointer rounded-md border p-2 ${target === t.id ? "border-accent-400 bg-accent-50" : "border-slate-200"}`}
                    >
                      <input type="radio" name="target" className="sr-only" checked={target === t.id} onChange={() => setTarget(t.id)} />
                      <span className="flex items-center justify-between gap-2">
                        <span className="font-medium text-slate-800">{t.name}</span>
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">{MODE_LABEL[t.mode]}</span>
                      </span>
                      <span className="mt-0.5 block text-[11px] leading-snug text-slate-500">{t.summary}</span>
                    </label>
                  ))}
                </div>
              </fieldset>
              <label className="block">
                <span className="mb-1 block font-semibold text-slate-700">Profile</span>
                <select className="am-input w-full" value={profile} onChange={(e) => setProfile(e.target.value)}>
                  {targets?.profiles.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.id}
                    </option>
                  ))}
                </select>
                <span className="mt-1 block text-[11px] text-slate-500">{targets?.profiles.find((p) => p.id === profile)?.summary}</span>
              </label>
              <label className="block">
                <span className="mb-1 block font-semibold text-slate-700">Settings (not secrets)</span>
                <textarea
                  className="am-input h-20 w-full font-mono"
                  placeholder={"LOG_LEVEL=info"}
                  value={envText}
                  onChange={(e) => setEnvText(e.target.value)}
                />
                {envErrors.map((e) => (
                  <span key={e} className="block text-red-700">
                    {e}
                  </span>
                ))}
              </label>
              {spec?.mode === "guided" && (
                <label className="block">
                  <span className="mb-1 block font-semibold text-slate-700">Image repository</span>
                  <input
                    className="am-input w-full font-mono"
                    placeholder="ghcr.io/acme/my-app"
                    value={image}
                    onChange={(e) => setImage(e.target.value)}
                  />
                </label>
              )}
            </div>

            {/* ---- plan ---- */}
            <div className="flex min-h-0 flex-col">
              <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
                {planning && !plan && <p className="text-slate-400">Checking compatibility…</p>}
                {planError && <div className="rounded border border-red-200 bg-red-50 p-2 text-red-800">{planError}</div>}
                {plan && (
                  <>
                    <div
                      className={`rounded-md border p-2 font-medium ${plan.compatible ? "border-emerald-200 bg-emerald-50 text-emerald-900" : "border-red-200 bg-red-50 text-red-900"}`}
                      role="status"
                    >
                      {plan.compatible
                        ? `Revision ${plan.revision} can be deployed to ${plan.target_name} (${plan.profile}).`
                        : `Revision ${plan.revision} cannot be deployed to ${plan.target_name} as it is.`}
                      {planning && <span className="ml-2 font-normal text-slate-500">updating…</span>}
                    </div>
                    <section className="space-y-1.5">
                      {bySeverity(plan.checks).map((c) => (
                        <div key={c.id} className={`rounded border p-2 ${SEVERITY_STYLE[c.severity]}`}>
                          <div className="font-semibold">
                            {SEVERITY_LABEL[c.severity]}: {c.title}
                          </div>
                          <div className="mt-0.5">{c.detail}</div>
                          {c.remedy && <div className="mt-0.5 italic">{c.remedy}</div>}
                        </div>
                      ))}
                    </section>
                    <section className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <h3 className="mb-1 font-semibold text-slate-700">Secrets (by name)</h3>
                        <ul className="space-y-0.5">
                          {plan.secrets.map((s) => (
                            <li key={s.name} className="font-mono">
                              {s.name}{" "}
                              <span className={s.provided === false ? "text-red-700" : s.provided ? "text-emerald-700" : "text-slate-400"}>
                                {s.provided === null ? "(set on the target)" : s.provided ? "✓ set on this server" : "✗ missing"}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                      <div>
                        <h3 className="mb-1 font-semibold text-slate-700">Needs</h3>
                        <ul className="list-disc space-y-0.5 pl-4 text-slate-600">
                          <li>~{plan.requirements.memory_mb} MB memory</li>
                          <li>
                            Requests up to {Math.round(plan.requirements.longest_request_seconds)} s ({plan.requirements.slowest_node})
                          </li>
                          <li>{plan.requirements.persistent_data.length ? "Persistent data directory" : "No persistent data"}</li>
                          {plan.requirements.network_egress && <li>Outbound network</li>}
                        </ul>
                      </div>
                    </section>
                    <section className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <h3 className="mb-1 font-semibold text-slate-700">What it will do</h3>
                        <ol className="list-decimal space-y-0.5 pl-4 text-slate-600">
                          {plan.steps.map((s) => (
                            <li key={s}>{s}</li>
                          ))}
                        </ol>
                      </div>
                      <div>
                        <h3 className="mb-1 font-semibold text-slate-700">Permissions it needs</h3>
                        <ul className="list-disc space-y-0.5 pl-4 text-slate-600">
                          {plan.permissions.map((s) => (
                            <li key={s}>{s}</li>
                          ))}
                        </ul>
                      </div>
                    </section>
                    <section>
                      <h3 className="mb-1 font-semibold text-slate-700">Artifacts</h3>
                      <div className="flex flex-wrap gap-1">
                        {plan.artifacts.map((a) => (
                          <button
                            key={a.path}
                            type="button"
                            className={`rounded border px-1.5 py-0.5 font-mono ${viewFile === a.path ? "border-accent-400 bg-accent-50" : "border-slate-200"}`}
                            onClick={() => setViewFile(viewFile === a.path ? null : a.path)}
                          >
                            {a.path.replace(/^deploy\//, "")} · {formatBytes(a.bytes)}
                          </button>
                        ))}
                      </div>
                      {viewFile && plan.files?.[viewFile] && (
                        <pre className="mt-2 max-h-72 overflow-auto rounded bg-slate-50 p-2 text-[11px] leading-snug text-slate-700">
                          {plan.files[viewFile]}
                        </pre>
                      )}
                    </section>
                    {spec && spec.facts.length > 0 && (
                      <section className="text-[11px] text-slate-500">
                        <h3 className="mb-0.5 font-semibold text-slate-600">
                          {spec.name} limits (verified {spec.verified_on})
                        </h3>
                        <ul className="list-disc pl-4">
                          {spec.facts.map((f) => (
                            <li key={f.text}>
                              {f.text}{" "}
                              {f.source && (
                                <a className="underline" href={f.source} target="_blank" rel="noreferrer">
                                  source
                                </a>
                              )}
                            </li>
                          ))}
                        </ul>
                      </section>
                    )}
                  </>
                )}
              </div>
              <div className="mt-3 flex items-center justify-between gap-3 border-t border-slate-100 pt-3">
                <label className="flex items-center gap-2 text-slate-700">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={!plan?.compatible || planning}
                    onChange={(e) => setConfirmed(e.target.checked)}
                  />
                  I reviewed this plan{plan ? ` (${plan.plan_hash.slice(5, 13)})` : ""}
                </label>
                <div className="flex gap-2">
                  <button type="button" className="am-btn" onClick={onClose}>
                    Close
                  </button>
                  <button
                    type="button"
                    className="am-btn-primary"
                    disabled={!plan?.compatible || !confirmed || busy || planning}
                    onClick={() => void deploy()}
                  >
                    {busy ? "Working…" : actionLabel}
                  </button>
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="mt-4 grid min-h-0 flex-1 gap-4 text-xs md:grid-cols-[1fr_22rem]">
            <div className="min-h-0 overflow-y-auto">
              {history.length === 0 && <p className="text-slate-400">No deployments yet.</p>}
              <table className="w-full border-collapse">
                <tbody>
                  {history.map((d) => (
                    <tr
                      key={d.id}
                      className={`cursor-pointer border-t border-slate-100 align-top ${selected?.id === d.id ? "bg-accent-50" : ""}`}
                      onClick={() => void api.getDeployment(d.id).then(setSelected)}
                    >
                      <td className="py-1.5 pr-2">
                        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${TONE_STYLE[statusTone(d.status)]}`}>
                          {STATUS_LABEL[d.status]}
                        </span>
                      </td>
                      <td className="py-1.5 pr-2 text-slate-800">
                        rev {d.revision} → {d.target_name}
                        <div className="text-[11px] text-slate-500">
                          {d.profile}
                          {d.rollback_of ? " · rollback" : ""} · {new Date(d.created_at).toLocaleString()}
                        </div>
                      </td>
                      <td className="py-1.5 pr-2">
                        {d.endpoint && (
                          <a className="font-mono text-accent-700 underline" href={`${d.endpoint}/docs`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
                            {d.endpoint.replace(/^https?:\/\//, "")}
                          </a>
                        )}
                        {d.health && (
                          <div className={d.health.ok ? "text-emerald-700" : "text-red-700"}>
                            {d.health.ok ? "healthy" : "unhealthy"} · {new Date(d.health.checked_at).toLocaleTimeString()}
                          </div>
                        )}
                      </td>
                      <td className="py-1.5 text-right">
                        {canRollBackTo(d, history) && (
                          <button type="button" className="am-btn" onClick={(e) => (e.stopPropagation(), rollBack(d))}>
                            Roll back to this
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <aside className="min-h-0 space-y-2 overflow-y-auto rounded-md border border-slate-200 p-3">
              {!selected ? (
                <p className="text-slate-400">Select a deployment.</p>
              ) : (
                <>
                  <div className="font-semibold text-slate-800">
                    {selected.id} · rev {selected.revision} → {selected.target_name}
                  </div>
                  {selected.error && <div className="rounded border border-red-200 bg-red-50 p-2 text-red-800">{selected.error}</div>}
                  <div className="text-slate-600">
                    Secrets: <span className="font-mono">{selected.secrets.join(", ") || "none"}</span>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    {selected.endpoint && (
                      <button type="button" className="am-btn" onClick={() => void act(() => api.checkDeploymentHealth(selected.id), "Health checked")}>
                        Check health
                      </button>
                    )}
                    {["running", "live", "artifacts_ready"].includes(selected.status) && (
                      <button type="button" className="am-btn" onClick={() => void act(() => api.stopDeployment(selected.id), "Stopped")}>
                        Stop
                      </button>
                    )}
                    <button
                      type="button"
                      className="am-btn"
                      onClick={() =>
                        void api.deploymentBundle(selected.id).then(({ blob, filename }) => save(blob, filename), (err) => notify(errorText(err), "error"))
                      }
                    >
                      Download files
                    </button>
                  </div>
                  {selected.mode === "guided" && selected.status === "artifacts_ready" && (
                    <div className="space-y-1">
                      <div className="text-slate-600">Deployed it? Record the URL to track health and history:</div>
                      <div className="flex gap-1">
                        <input className="am-input min-w-0 flex-1 font-mono" placeholder="https://…" value={urlDraft} onChange={(e) => setUrlDraft(e.target.value)} />
                        <button type="button" className="am-btn" onClick={() => void act(() => api.registerDeploymentUrl(selected.id, urlDraft), "Marked live")}>
                          Mark live
                        </button>
                      </div>
                    </div>
                  )}
                  {selected.log ? (
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-[10px] leading-snug text-slate-700">
                      {selected.log.split("\n").slice(-80).join("\n")}
                    </pre>
                  ) : (
                    selected.mode === "direct" && <p className="text-slate-400">No log yet.</p>
                  )}
                </>
              )}
            </aside>
          </div>
        )}
      </div>
    </div>
  );
}

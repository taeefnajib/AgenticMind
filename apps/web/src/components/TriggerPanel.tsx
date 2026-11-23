import { useEffect, useState } from "react";

import { api } from "../api/client";
import { setupHint, triggerUrl, type TriggerStatus } from "../lib/triggers";

/** Live status of a trigger node: active or what it needs, its URL, next run, recent events and errors. */
export function TriggerPanel({ projectId, nodeId }: { projectId: string | null; nodeId: string }) {
  const [status, setStatus] = useState<TriggerStatus | null | undefined>(undefined);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    const load = () =>
      api
        .triggers(projectId)
        .then((all) => alive && setStatus(all.find((t) => t.node_id === nodeId) ?? null))
        .catch(() => alive && setStatus(null));
    void load();
    const timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [projectId, nodeId]);

  if (status === undefined) return <p className="text-xs text-slate-500">Loading…</p>;
  if (status === null) {
    return (
      <p className="text-xs text-slate-500">
        Not listening. Tick <em>Enabled</em> and save: triggers run from the latest saved revision.
      </p>
    );
  }
  const url = triggerUrl(window.location.origin, status.path);
  const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${status.active ? "bg-emerald-500" : "bg-amber-500"}`} />
        <span className="font-medium text-slate-800">{status.active ? "Listening" : "Not ready"}</span>
        <span className="text-slate-500">revision {status.revision}</span>
      </div>
      {status.problem && <p className="text-amber-700">{status.problem.charAt(0).toUpperCase() + status.problem.slice(1)}.</p>}
      {url && <code className="block break-all rounded bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700">{url}</code>}
      {status.next_run_at && <p className="text-slate-600">Next run: {when(status.next_run_at)}</p>}
      <p className="text-slate-600">
        Events: {status.events} · last: {when(status.last_event_at)}
        {status.last_run_id && <span className="font-mono text-slate-500"> · {status.last_run_id}</span>}
      </p>
      {status.last_error && <p className="break-words text-rose-700">Last error: {status.last_error}</p>}
      <p className="text-slate-500">{setupHint(status, url)}</p>
      {url && url.includes("localhost") && (
        <p className="text-slate-500">Services on the internet cannot reach localhost: use a tunnel (e.g. ngrok, Cloudflare Tunnel) or a public host.</p>
      )}
    </div>
  );
}

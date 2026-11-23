import { useCallback, useEffect, useState } from "react";

import { git as gitApi, type GitCommit, type GitLink, type GitState } from "../api/client";
import { errorMessage } from "./AuthGate";

const EMPTY: GitLink = {
  repo: "", branch: "main", folder: "", token_secret: "GITHUB_TOKEN", auto_push: false, include_app: false,
  create_repo: false, private: true,
};

/** Project panel → GitHub: push the project to a repository, pull, history and restore (ADR-0038). */
export function GitPanel({ projectId, dirty, onReload }: { projectId: string | null; dirty: boolean; onReload: () => void }) {
  const [state, setState] = useState<GitState | null>(null);
  const [form, setForm] = useState<GitLink>(EMPTY);
  const [editing, setEditing] = useState(false);
  const [message, setMessage] = useState("");
  const [history, setHistory] = useState<GitCommit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; error?: boolean; force?: boolean } | null>(null);

  const load = useCallback(() => {
    if (!projectId) return;
    gitApi.get(projectId).then((s) => {
      setState(s);
      setForm(s.link ?? EMPTY);
      setEditing(!s.link);
    }, (err) => setNote({ text: errorMessage(err), error: true }));
  }, [projectId]);
  useEffect(() => {
    setHistory(null);
    setNote(null);
    load();
  }, [load]);

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
    } catch (err) {
      const text = errorMessage(err);
      setNote({ text, error: true, force: text.includes("changed on GitHub") });
    } finally {
      setBusy(false);
    }
  };

  if (!projectId) return null;
  const pid = projectId;
  const st = state?.state ?? {};
  const set = (patch: Partial<GitLink>) => setForm({ ...form, ...patch });

  if (editing || !state?.link) {
    return (
      <div className="space-y-2 text-xs">
        <p className="text-slate-500">Keep this project in a GitHub repository: every push is one commit with the canonical project file and a README.</p>
        <input className="am-input" placeholder="owner/repository" value={form.repo} onChange={(e) => set({ repo: e.target.value })} />
        <div className="grid grid-cols-2 gap-2">
          <input className="am-input" placeholder="branch" value={form.branch} onChange={(e) => set({ branch: e.target.value })} />
          <input className="am-input" placeholder="folder (optional)" value={form.folder} onChange={(e) => set({ folder: e.target.value })} />
        </div>
        <input className="am-input font-mono" title="Secret holding a GitHub token with Contents: read and write" value={form.token_secret}
          onChange={(e) => set({ token_secret: e.target.value.toUpperCase() })} />
        <p className="text-slate-500">Token secret: a fine-grained token with <em>Contents: read and write</em> on the repository (add it in Settings → Secrets).</p>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.auto_push} onChange={(e) => set({ auto_push: e.target.checked })} /> Push automatically on every save</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.include_app} onChange={(e) => set({ include_app: e.target.checked })} /> Also commit the exported app (app/)</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.create_repo} onChange={(e) => set({ create_repo: e.target.checked })} /> Create the repository if it does not exist (private)</label>
        {note && <p className={note.error ? "text-rose-700" : "text-emerald-700"}>{note.text}</p>}
        <div className="flex gap-2">
          <button className="am-btn-primary" disabled={busy || !form.repo} onClick={() => void act(async () => {
            setState(await gitApi.link(pid, form));
            setEditing(false);
          })}>Link</button>
          <button className="am-btn" disabled={busy || !form.repo} onClick={() => void act(async () => {
            const r = await gitApi.check(pid, form);
            setNote({ text: r.exists ? `Reachable${r.can_push === false ? ", but this token cannot push" : ""} (default branch ${r.default_branch})` : "Not found: it will be created on the first push" });
          })}>Check access</button>
          {state?.link && <button className="am-btn" onClick={() => setEditing(false)}>Cancel</button>}
        </div>
      </div>
    );
  }

  const link = state.link;
  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center justify-between gap-2">
        <a className="truncate font-mono text-accent-700 underline" href={state.repo_url ?? undefined} target="_blank" rel="noreferrer">
          {link.repo}
        </a>
        <button className="text-slate-500 underline" onClick={() => setEditing(true)}>Edit</button>
      </div>
      <p className="text-slate-500">{link.branch} · {state.file}{link.auto_push ? " · auto-push" : ""}</p>
      {!state.token_configured && <p className="text-amber-700">Add the secret {link.token_secret} to push.</p>}
      {st.commit && (
        <p className="text-slate-600">
          Last sync: revision {st.revision} → <a className="font-mono text-accent-700 underline" href={st.url ?? undefined} target="_blank" rel="noreferrer">{st.commit.slice(0, 7)}</a>
          {st.synced_at && ` · ${new Date(st.synced_at).toLocaleString()}`}
        </p>
      )}
      {st.error && <p className="text-rose-700">{st.error}</p>}
      {dirty && <p className="text-amber-700">Save first: pushes send the latest saved revision.</p>}
      <input className="am-input" placeholder="Commit message (optional)" value={message} onChange={(e) => setMessage(e.target.value)} />
      {note && <p className={note.error ? "text-rose-700" : "text-emerald-700"}>{note.text}</p>}
      <div className="flex flex-wrap gap-2">
        <button className="am-btn-primary" disabled={busy || dirty} onClick={() => void act(async () => {
          const r = await gitApi.push(pid, { message });
          setNote({ text: r.status === "up_to_date" ? "Already up to date" : `Pushed ${r.commit?.slice(0, 7)}` });
          setMessage("");
          load();
        })}>Push</button>
        {note?.force && (
          <button className="am-btn" disabled={busy} onClick={() => void act(async () => {
            const r = await gitApi.push(pid, { message, force: true });
            setNote({ text: `Pushed ${r.commit?.slice(0, 7)} on top of the GitHub change` });
            load();
          })}>Push anyway</button>
        )}
        <button className="am-btn" disabled={busy} onClick={() => void act(async () => {
          const r = await gitApi.pull(pid);
          setNote({ text: r.status === "up_to_date" ? "Already up to date" : `Pulled as revision ${r.revision}` });
          onReload();
          load();
        })}>Pull</button>
        <button className="am-btn" disabled={busy} onClick={() => void act(async () => setHistory(await gitApi.history(pid)))}>History</button>
      </div>
      {history && (
        <ul className="max-h-48 space-y-1 overflow-y-auto">
          {history.length === 0 && <li className="text-slate-500">No commits yet.</li>}
          {history.map((c) => (
            <li key={c.sha} className="flex items-start justify-between gap-2 border-t border-slate-100 pt-1">
              <span className="min-w-0">
                <a className="font-mono text-accent-700 underline" href={c.url ?? undefined} target="_blank" rel="noreferrer">{c.sha.slice(0, 7)}</a>{" "}
                <span className="text-slate-700">{c.message.split("\n")[0]}</span>
                <span className="block text-slate-500">{c.author} · {c.date ? new Date(c.date).toLocaleString() : ""}</span>
              </span>
              <button className="shrink-0 text-slate-600 underline" disabled={busy} title="Load this commit as a new revision"
                onClick={() => void act(async () => {
                  const r = await gitApi.restore(pid, c.sha);
                  setNote({ text: r.status === "up_to_date" ? "That version is already current" : `Restored ${c.sha.slice(0, 7)} as revision ${r.revision}` });
                  onReload();
                })}>Restore</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

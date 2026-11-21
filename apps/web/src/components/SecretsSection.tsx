import { useCallback, useEffect, useState, type FormEvent } from "react";

import { secrets as secretsApi } from "../api/client";
import type { WorkspaceSecret } from "../api/types";
import { errorMessage, Field } from "./AuthGate";

/** Mirrors agenticmind_api.secrets_store.SECRET_NAME_RE. */
export const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;

/** "openai key" → "OPENAI_KEY": the form nudges names into the conventional env-var style as you type
 * (no trimming per keystroke, or the space before the next word would be lost). */
export function normaliseSecretName(raw: string): string {
  return raw.replace(/^\s+/, "").replace(/[\s-]+/g, "_").replace(/[^A-Za-z0-9_]/g, "").toUpperCase();
}

export function SecretsSection({ notify }: { notify: (text: string, tone?: "info" | "error" | "success") => void }) {
  const [list, setList] = useState<WorkspaceSecret[] | null>(null);
  const [canEdit, setCanEdit] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [multiline, setMultiline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  const load = useCallback(() => {
    secretsApi.list().then(
      (r) => {
        setList(r.secrets);
        setCanEdit(r.can_edit);
      },
      (err) => setError(errorMessage(err)),
    );
  }, []);
  useEffect(load, [load]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const saved = await secretsApi.save(name, value);
      notify(`Saved ${saved.name}. Nodes can now use {"$secret": "${saved.name}"}.`, "success");
      setName("");
      setValue("");
      load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (n: string) => {
    try {
      await secretsApi.remove(n);
      notify(`Deleted ${n}`, "success");
      setConfirmDelete(null);
      load();
    } catch (err) {
      notify(errorMessage(err), "error");
    }
  };

  const validName = SECRET_NAME.test(name);
  const exists = list?.some((s) => s.name === name) ?? false;

  return (
    <>
      <div className="mb-5 pr-8">
        <h3 className="text-base font-semibold text-slate-900">Secrets</h3>
        <p className="mt-1 text-xs leading-relaxed text-slate-500">
          Passwords, tokens and keys for databases, storage and model providers. Values are encrypted on the server
          and never shown again; nodes refer to them by name, e.g. a Database Connector password set to
          <code className="mx-1 rounded bg-slate-100 px-1 font-mono">SUPABASE_PASSWORD</code>. A variable with the same
          name in the server’s environment (<code className="font-mono">.env</code>) takes precedence.
        </p>
      </div>
      {error && <div className="mb-3 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">{error}</div>}

      {canEdit ? (
        <form onSubmit={(e) => void save(e)} className="mb-5 rounded-xl border border-slate-200">
          <div className="border-b border-slate-100 px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
            Add or replace a secret
          </div>
          <div className="space-y-3 p-4">
            <Field label="Name" hint={exists ? "A secret with this name exists: saving replaces its value." : "Letters, digits and _, e.g. SNOWFLAKE_PASSWORD."}>
              <input
                className="am-input font-mono text-[13px]"
                value={name}
                onChange={(e) => setName(normaliseSecretName(e.target.value))}
                placeholder="DATABRICKS_TOKEN"
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
            <Field
              label="Value"
              hint={
                <button type="button" className="text-accent-700 hover:underline" onClick={() => setMultiline((m) => !m)}>
                  {multiline ? "Single line (hidden)" : "Multi-line (PEM keys, service-account JSON)"}
                </button>
              }
            >
              {multiline ? (
                <textarea
                  className="am-input min-h-[110px] font-mono text-[11px]"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={'-----BEGIN PRIVATE KEY-----\n…   or   {"type": "service_account", …}'}
                />
              ) : (
                <input
                  className="am-input font-mono text-[13px]"
                  type="password"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  autoComplete="new-password"
                  spellCheck={false}
                />
              )}
            </Field>
          </div>
          <div className="flex justify-end border-t border-slate-100 px-4 py-2.5">
            <button type="submit" className="am-btn-primary" disabled={busy || !validName || !value}>
              {busy ? "Saving…" : exists ? "Replace secret" : "Save secret"}
            </button>
          </div>
        </form>
      ) : (
        <p className="mb-4 rounded-md bg-slate-50 px-3 py-2 text-xs text-slate-600">Only admins can add or change secrets.</p>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2 font-semibold">Name</th>
              <th className="px-3 py-2 font-semibold">Updated</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {(list ?? []).map((s) => (
              <tr key={s.name}>
                <td className="px-3 py-2">
                  <div className="font-mono text-[13px] text-slate-900">{s.name}</div>
                  {s.overridden_by_environment && (
                    <div className="text-[11px] text-amber-700">The server environment defines this name too; that value is used.</div>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {s.updated_at ? new Date(s.updated_at).toLocaleString() : "–"}
                  {s.updated_by && ` · ${s.updated_by}`}
                </td>
                <td className="px-3 py-2 text-right">
                  {canEdit &&
                    (confirmDelete === s.name ? (
                      <span className="inline-flex items-center gap-1.5 text-xs">
                        <span className="text-red-700">Delete? Nodes using it will fail.</span>
                        <button type="button" className="am-btn !px-2 !py-0.5 text-xs text-red-600" onClick={() => void remove(s.name)}>
                          Delete
                        </button>
                        <button type="button" className="am-btn !px-2 !py-0.5 text-xs" onClick={() => setConfirmDelete(null)}>
                          Keep
                        </button>
                      </span>
                    ) : (
                      <span className="inline-flex gap-1.5">
                        <button type="button" className="am-btn !px-2 !py-0.5 text-xs" onClick={() => setName(s.name)}>
                          Replace
                        </button>
                        <button type="button" className="am-btn !px-2 !py-0.5 text-xs text-red-600" onClick={() => setConfirmDelete(s.name)}>
                          Delete
                        </button>
                      </span>
                    ))}
                </td>
              </tr>
            ))}
            {list !== null && list.length === 0 && (
              <tr>
                <td colSpan={3} className="px-3 py-4 text-center text-xs text-slate-500">
                  No secrets saved here yet. Secrets from the server’s environment still work.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}

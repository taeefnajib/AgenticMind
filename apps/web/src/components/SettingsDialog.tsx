import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

import { api, ApiError, auth, settings as settingsApi } from "../api/client";
import type {
  AssistantConfig,
  AssistantSettings,
  AssistantTestResult,
  ProjectSummary,
  ProviderModel,
  Role,
  User,
} from "../api/types";
import type { ThemePreference } from "../lib/useTheme";
import { errorMessage, Field, useAuth } from "./AuthGate";
import { Avatar } from "./UserMenu";

export type SettingsSection = "profile" | "projects" | "assistant" | "people" | "appearance";

interface Props {
  open: boolean;
  section: SettingsSection;
  onSection: (s: SettingsSection) => void;
  onClose: () => void;
  theme: ThemePreference;
  onTheme: (t: ThemePreference) => void;
  notify: (text: string, tone?: "info" | "error" | "success") => void;
  currentProjectId: string | null;
  onOpenProject: (id: string) => void;
  /** After a project was deleted (reload the list; switch away if it was open). */
  onProjectDeleted: (id: string) => Promise<void>;
}

const PROVIDER_LABEL: Record<string, string> = {
  openrouter: "OpenRouter",
  openai_compatible: "OpenAI-compatible API",
  mock: "Mock (offline, deterministic)",
};

export function SettingsDialog(p: Props) {
  const { user, mode } = useAuth();
  const isAdmin = mode === "off" || user?.role === "admin";
  const sections: { id: SettingsSection; label: string; show: boolean }[] = [
    { id: "profile", label: mode === "off" ? "Account" : "Profile", show: true },
    { id: "projects", label: "Projects", show: true },
    { id: "assistant", label: "AI assistant", show: true },
    { id: "people", label: "People", show: mode === "required" && isAdmin },
    { id: "appearance", label: "Appearance", show: true },
  ];

  useEffect(() => {
    if (!p.open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && p.onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [p.open, p.onClose]);

  if (!p.open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/30 p-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      onMouseDown={(e) => e.target === e.currentTarget && p.onClose()}
    >
      <div className="flex h-[min(680px,calc(100vh-2rem))] w-full max-w-4xl overflow-hidden rounded-2xl bg-surface shadow-2xl">
        <nav className="flex w-48 shrink-0 flex-col border-r border-slate-200 bg-slate-50 p-3" aria-label="Settings sections">
          <h2 id="settings-title" className="px-2 pb-3 pt-1 text-sm font-semibold text-slate-900">
            Settings
          </h2>
          {sections
            .filter((s) => s.show)
            .map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => p.onSection(s.id)}
                aria-current={p.section === s.id ? "page" : undefined}
                className={`mb-0.5 rounded-md px-2 py-1.5 text-left text-sm transition ${
                  p.section === s.id
                    ? "bg-surface font-medium text-slate-900 shadow-sm ring-1 ring-slate-200"
                    : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                }`}
              >
                {s.label}
              </button>
            ))}
        </nav>
        <div className="relative min-w-0 flex-1 overflow-y-auto p-6">
          <button
            type="button"
            className="absolute right-3 top-3 rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            onClick={p.onClose}
            aria-label="Close settings"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
          {p.section === "profile" && <ProfileSection notify={p.notify} />}
          {p.section === "projects" && (
            <ProjectsSection
              currentProjectId={p.currentProjectId}
              onOpenProject={p.onOpenProject}
              onProjectDeleted={p.onProjectDeleted}
              notify={p.notify}
            />
          )}
          {p.section === "assistant" && <AssistantSection notify={p.notify} />}
          {p.section === "people" && isAdmin && mode === "required" && <PeopleSection notify={p.notify} />}
          {p.section === "appearance" && <AppearanceSection theme={p.theme} onTheme={p.onTheme} />}
        </div>
      </div>
    </div>
  );
}

function SectionHeader({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="mb-5 pr-8">
      <h3 className="text-base font-semibold text-slate-900">{title}</h3>
      {children && <p className="mt-1 text-xs leading-relaxed text-slate-500">{children}</p>}
    </div>
  );
}

function Card({ title, children, footer }: { title: string; children: ReactNode; footer?: ReactNode }) {
  return (
    <section className="mb-5 rounded-xl border border-slate-200">
      <div className="border-b border-slate-100 px-4 py-2.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </div>
      <div className="space-y-3 p-4">{children}</div>
      {footer && <div className="flex items-center justify-end gap-2 border-t border-slate-100 px-4 py-2.5">{footer}</div>}
    </section>
  );
}

function Alert({ tone, children }: { tone: "error" | "success" | "info" | "warn"; children: ReactNode }) {
  const cls = {
    error: "bg-red-50 text-red-700",
    success: "bg-emerald-50 text-emerald-700",
    info: "bg-accent-100 text-accent-700",
    warn: "bg-amber-50 text-amber-800",
  }[tone];
  return (
    <div className={`rounded-md px-3 py-2 text-xs ${cls}`} role={tone === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}

const fmtDate = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

// ---- Profile ----------------------------------------------------------------------------------
function ProfileSection({ notify }: { notify: Props["notify"] }) {
  const { user, mode, setUser, signOut } = useAuth();
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [sessions, setSessions] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pw, setPw] = useState({ current: "", next: "", confirm: "" });
  const [pwMsg, setPwMsg] = useState<{ tone: "error" | "success"; text: string } | null>(null);

  useEffect(() => {
    if (mode === "required") auth.me().then((me) => setSessions(me.sessions ?? null), () => undefined);
  }, [mode]);

  if (mode === "off" || !user) {
    return (
      <>
        <SectionHeader title="Account" />
        <Alert tone="warn">
          Sign-in is turned off (<code className="font-mono">AGENTICMIND_AUTH=off</code>), so everyone who can reach this
          API uses it as the same local user. Remove that setting and restart the API to create accounts.
        </Alert>
      </>
    );
  }

  const dirty = displayName.trim() !== user.display_name || (email.trim() || null) !== user.email;

  const saveProfile = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setUser(await auth.updateMe({ display_name: displayName, email: email.trim() || null }));
      notify("Profile saved", "success");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const changePassword = async (e: FormEvent) => {
    e.preventDefault();
    setPwMsg(null);
    if (pw.next !== pw.confirm) {
      setPwMsg({ tone: "error", text: "The new passwords don’t match." });
      return;
    }
    setBusy(true);
    try {
      await auth.changePassword(pw.current, pw.next);
      setPw({ current: "", next: "", confirm: "" });
      setSessions(1);
      setPwMsg({ tone: "success", text: "Password changed. Other browsers were signed out." });
    } catch (err) {
      setPwMsg({ tone: "error", text: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };

  const signOutOthers = async () => {
    try {
      const { revoked } = await auth.revokeOtherSessions();
      setSessions(1);
      notify(revoked ? `Signed out ${revoked} other browser${revoked === 1 ? "" : "s"}` : "No other browsers were signed in", "success");
    } catch (err) {
      notify(errorMessage(err), "error");
    }
  };

  return (
    <>
      <SectionHeader title="Profile" />
      <div className="mb-5 flex items-center gap-4">
        <Avatar user={user} size="lg" />
        <div className="min-w-0">
          <div className="truncate text-lg font-semibold text-slate-900">{user.display_name}</div>
          <div className="text-xs text-slate-500">
            @{user.username} ·{" "}
            <span className={`rounded px-1.5 py-0.5 font-medium ${user.role === "admin" ? "bg-accent-100 text-accent-700" : "bg-slate-100 text-slate-600"}`}>
              {user.role === "admin" ? "Admin" : "Member"}
            </span>{" "}
            · joined {new Date(user.created_at).toLocaleDateString()}
          </div>
        </div>
      </div>

      <form onSubmit={(e) => void saveProfile(e)}>
        <Card
          title="Details"
          footer={
            <button type="submit" className="am-btn-primary" disabled={busy || !dirty || !displayName.trim()}>
              Save profile
            </button>
          }
        >
          {error && <Alert tone="error">{error}</Alert>}
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Display name">
              <input className="am-input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} autoComplete="name" />
            </Field>
            <Field label="Email (optional)">
              <input className="am-input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
            </Field>
          </div>
          <Field label="Username" hint="Usernames can’t be changed.">
            <input className="am-input bg-slate-50 text-slate-500" value={user.username} readOnly />
          </Field>
        </Card>
      </form>

      <form onSubmit={(e) => void changePassword(e)}>
        <Card
          title="Password"
          footer={
            <button type="submit" className="am-btn" disabled={busy || !pw.current || !pw.next}>
              Change password
            </button>
          }
        >
          {pwMsg && <Alert tone={pwMsg.tone}>{pwMsg.text}</Alert>}
          {/* hidden username helps password managers pair the new password with this account */}
          <input type="text" autoComplete="username" value={user.username} readOnly hidden />
          <Field label="Current password">
            <input className="am-input" type="password" autoComplete="current-password" value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="New password" hint="At least 8 characters.">
              <input className="am-input" type="password" autoComplete="new-password" value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} />
            </Field>
            <Field label="Confirm new password">
              <input className="am-input" type="password" autoComplete="new-password" value={pw.confirm} onChange={(e) => setPw({ ...pw, confirm: e.target.value })} />
            </Field>
          </div>
        </Card>
      </form>

      <Card title="Sessions">
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm text-slate-700">
          <span>
            Signed in on {sessions ?? "…"} browser{sessions === 1 ? "" : "s"}. Last sign-in {fmtDate(user.last_login_at)}.
          </span>
          <div className="flex gap-2">
            <button type="button" className="am-btn" onClick={() => void signOutOthers()} disabled={sessions === 1}>
              Sign out other browsers
            </button>
            <button type="button" className="am-btn text-red-600" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </div>
      </Card>
    </>
  );
}

// ---- AI assistant -----------------------------------------------------------------------------
function AssistantSection({ notify }: { notify: Props["notify"] }) {
  const [state, setState] = useState<AssistantSettings | null>(null);
  const [form, setForm] = useState<AssistantConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [fieldError, setFieldError] = useState<{ field: string; message: string } | null>(null);
  const [busy, setBusy] = useState<"save" | "reset" | "test" | "models" | null>(null);
  const [test, setTest] = useState<AssistantTestResult | null>(null);
  const [models, setModels] = useState<ProviderModel[] | null>(null);
  const [modelQuery, setModelQuery] = useState("");
  const [modelsError, setModelsError] = useState<string | null>(null);

  const apply = useCallback((s: AssistantSettings) => {
    setState(s);
    setForm(s.assistant);
    setFieldError(null);
  }, []);

  useEffect(() => {
    settingsApi.assistant().then(apply, (err) => setLoadError(errorMessage(err)));
  }, [apply]);

  const needsKey = useMemo(
    () => state?.providers.find((p) => p.name === form?.provider)?.needs_key ?? false,
    [state, form?.provider],
  );

  const filteredModels = useMemo(() => {
    if (!models) return [];
    const q = modelQuery.trim().toLowerCase();
    return (q ? models.filter((m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)) : models).slice(0, 80);
  }, [models, modelQuery]);

  if (loadError) return <Alert tone="error">{loadError}</Alert>;
  if (!state || !form) return <div className="text-sm text-slate-500">Loading…</div>;
  const readOnly = !state.can_edit;
  const dirty = JSON.stringify(form) !== JSON.stringify(state.assistant);
  const set = (patch: Partial<AssistantConfig>) => {
    setForm({ ...form, ...patch });
    setTest(null);
    if (fieldError && Object.keys(patch).includes(fieldError.field)) setFieldError(null);
  };

  const run = async <T,>(kind: NonNullable<typeof busy>, fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(kind);
    try {
      return await fn();
    } catch (err) {
      const d = err instanceof ApiError ? (err.detail as { field?: string; message?: string } | undefined) : undefined;
      if (d?.field) setFieldError({ field: d.field, message: d.message ?? "Invalid value" });
      else notify(errorMessage(err), "error");
      return undefined;
    } finally {
      setBusy(null);
    }
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const s = await run("save", () => settingsApi.saveAssistant(form));
    if (s) {
      apply(s);
      notify(`AI assistant now uses ${s.assistant.model}`, "success");
    }
  };
  const reset = async () => {
    const s = await run("reset", () => settingsApi.resetAssistant());
    if (s) {
      apply(s);
      notify("AI assistant settings reset to the server defaults", "success");
    }
  };
  const testNow = async () => {
    setTest(null);
    const r = await run("test", () => settingsApi.testAssistant(form));
    if (r) setTest(r);
  };
  const browse = async () => {
    setModelsError(null);
    setBusy("models");
    try {
      const r = await settingsApi.models(form);
      setModels(r.models);
    } catch (err) {
      setModelsError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  };
  const err = (field: string) =>
    fieldError?.field === field ? <span className="mt-1 block text-[11px] text-red-600">{fieldError.message}</span> : null;

  return (
    <form onSubmit={(e) => void save(e)}>
      <SectionHeader title="AI assistant">
        The ✨ assistant turns plain-language requests into workflow changes. Choose the provider and model it uses for
        everyone in this workspace.
      </SectionHeader>

      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
        <span className={`rounded-full px-2 py-0.5 font-medium ${state.source === "workspace" ? "bg-accent-100 text-accent-700" : "bg-slate-100 text-slate-600"}`}>
          {state.source === "workspace" ? "Workspace setting" : "Server default (.env)"}
        </span>
        {state.updated_at && state.source === "workspace" && (
          <span className="text-slate-500">changed {fmtDate(state.updated_at)}</span>
        )}
        {readOnly && <span className="text-slate-500">Only admins can change these settings.</span>}
      </div>

      <Card
        title="Model"
        footer={
          !readOnly && (
            <>
              {state.source === "workspace" && (
                <button type="button" className="am-btn mr-auto" onClick={() => void reset()} disabled={busy !== null}>
                  Reset to server default
                </button>
              )}
              <button type="button" className="am-btn" onClick={() => void testNow()} disabled={busy !== null}>
                {busy === "test" ? "Testing…" : "Test connection"}
              </button>
              <button type="submit" className="am-btn-primary" disabled={busy !== null || !dirty}>
                {busy === "save" ? "Saving…" : "Save"}
              </button>
            </>
          )
        }
      >
        <fieldset disabled={readOnly} className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Provider">
              <select
                className="am-input"
                value={form.provider}
                onChange={(e) => {
                  set({ provider: e.target.value });
                  setModels(null);
                }}
              >
                {state.providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {PROVIDER_LABEL[p.name] ?? p.name}
                  </option>
                ))}
              </select>
              {err("provider")}
            </Field>
            <Field label="Model" hint={form.provider === "openrouter" ? "An OpenRouter model id, e.g. google/gemini-3.8-flash." : undefined}>
              <div className="flex gap-1.5">
                <input
                  className="am-input font-mono text-[13px]"
                  value={form.model}
                  onChange={(e) => set({ model: e.target.value })}
                  spellCheck={false}
                  aria-invalid={fieldError?.field === "model"}
                />
                {!readOnly && (
                  <button type="button" className="am-btn !px-2.5" onClick={() => void browse()} disabled={busy !== null} title="List the models this provider offers">
                    {busy === "models" ? "…" : "Browse"}
                  </button>
                )}
              </div>
              {err("model")}
            </Field>
          </div>

          {modelsError && <Alert tone="error">{modelsError}</Alert>}
          {models && (
            <div className="rounded-lg border border-slate-200">
              <div className="flex items-center gap-2 border-b border-slate-100 p-2">
                <input
                  className="am-input"
                  placeholder={`Search ${models.length} models…`}
                  value={modelQuery}
                  onChange={(e) => setModelQuery(e.target.value)}
                  autoFocus
                />
                <button type="button" className="am-btn" onClick={() => setModels(null)}>
                  Close
                </button>
              </div>
              <ul className="max-h-52 overflow-y-auto py-1 text-sm" role="listbox" aria-label="Models">
                {filteredModels.map((m) => (
                  <li key={m.id}>
                    <button
                      type="button"
                      role="option"
                      aria-selected={m.id === form.model}
                      className={`flex w-full items-baseline justify-between gap-3 px-3 py-1.5 text-left hover:bg-slate-50 ${m.id === form.model ? "bg-accent-100/60" : ""}`}
                      onClick={() => {
                        set({ model: m.id });
                        setModels(null);
                        setModelQuery("");
                      }}
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-slate-900">{m.name}</span>
                        <span className="block truncate font-mono text-[11px] text-slate-500">{m.id}</span>
                      </span>
                      {m.context_length ? (
                        <span className="shrink-0 text-[11px] text-slate-400">{Math.round(m.context_length / 1000)}k ctx</span>
                      ) : null}
                    </button>
                  </li>
                ))}
                {filteredModels.length === 0 && <li className="px-3 py-2 text-xs text-slate-500">No models match.</li>}
              </ul>
            </div>
          )}

          {needsKey && (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="API key secret"
                hint={
                  <>
                    The <em>name</em> of the secret holding the key. Set its value in the API’s environment or{" "}
                    <code className="font-mono">.env</code>; keys are never stored in settings.
                  </>
                }
              >
                <input
                  className="am-input font-mono text-[13px]"
                  value={form.api_key_secret}
                  onChange={(e) => set({ api_key_secret: e.target.value.toUpperCase() })}
                  spellCheck={false}
                />
                {err("api_key_secret")}
              </Field>
              <Field
                label={form.provider === "openai_compatible" ? "Base URL" : "Base URL (optional)"}
                hint={form.provider === "openrouter" ? "Leave empty for https://openrouter.ai/api/v1." : "The API root, ending before /chat/completions."}
              >
                <input
                  className="am-input font-mono text-[13px]"
                  value={form.base_url ?? ""}
                  onChange={(e) => set({ base_url: e.target.value || null })}
                  placeholder={form.provider === "openrouter" ? "https://openrouter.ai/api/v1" : "https://…/v1"}
                  spellCheck={false}
                />
                {err("base_url")}
              </Field>
            </div>
          )}

          <Field label="Repair attempts" hint="How many times the assistant may fix a graph that fails validation before giving up.">
            <select className="am-input !w-28" value={form.max_attempts} onChange={(e) => set({ max_attempts: Number(e.target.value) })}>
              {[1, 2, 3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            {err("max_attempts")}
          </Field>
        </fieldset>

        {needsKey && !state.configured && !dirty && (
          <Alert tone="warn">
            The secret <code className="font-mono">{state.assistant.api_key_secret}</code> is not set on the server, so the
            assistant can’t answer yet.
          </Alert>
        )}
        {test?.ok === true && (
          <Alert tone="success">
            Connected: <span className="font-mono">{test.model}</span> answered “{test.reply.trim() || "(empty)"}” in{" "}
            {(test.latency_ms / 1000).toFixed(1)} s.
          </Alert>
        )}
        {test?.ok === false && (
          <Alert tone="error">
            {test.message} <span className="font-mono opacity-70">({test.code})</span>
          </Alert>
        )}
      </Card>

      {state.source === "workspace" && (
        <p className="text-[11px] text-slate-500">
          Server default: <span className="font-mono">{PROVIDER_LABEL[state.defaults.provider] ?? state.defaults.provider}</span> ·{" "}
          <span className="font-mono">{state.defaults.model}</span>
        </p>
      )}
    </form>
  );
}

// ---- People (admins) --------------------------------------------------------------------------
function PeopleSection({ notify }: { notify: Props["notify"] }) {
  const { user: me } = useAuth();
  const [users, setUsers] = useState<User[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ username: "", display_name: "", password: "", role: "member" as Role });
  const [error, setError] = useState<string | null>(null);
  const [resetFor, setResetFor] = useState<string | null>(null);
  const [resetPw, setResetPw] = useState("");

  const load = useCallback(() => {
    auth.users().then(setUsers, (err) => setError(errorMessage(err)));
  }, []);
  useEffect(load, [load]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      const u = await auth.createUser({ ...draft, display_name: draft.display_name || draft.username });
      notify(`Added ${u.display_name}. Share the first password with them privately.`, "success");
      setDraft({ username: "", display_name: "", password: "", role: "member" });
      setAdding(false);
      load();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const update = async (u: User, body: Parameters<typeof auth.updateUser>[1], done: string) => {
    try {
      await auth.updateUser(u.id, body);
      notify(done, "success");
      load();
      return true;
    } catch (err) {
      notify(errorMessage(err), "error");
      return false;
    }
  };

  return (
    <>
      <SectionHeader title="People">
        Everyone here shares the workspace’s projects. Admins can also add people, change workspace settings and approve
        node packages.
      </SectionHeader>
      {error && <div className="mb-3"><Alert tone="error">{error}</Alert></div>}

      <div className="mb-3 flex justify-end">
        {!adding && (
          <button type="button" className="am-btn-primary" onClick={() => setAdding(true)}>
            Add person
          </button>
        )}
      </div>
      {adding && (
        <form onSubmit={(e) => void add(e)}>
          <Card
            title="New account"
            footer={
              <>
                <button type="button" className="am-btn" onClick={() => setAdding(false)}>
                  Cancel
                </button>
                <button type="submit" className="am-btn-primary" disabled={!draft.username || !draft.password}>
                  Create account
                </button>
              </>
            }
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Username">
                <input className="am-input" value={draft.username} onChange={(e) => setDraft({ ...draft, username: e.target.value })} autoComplete="off" autoFocus />
              </Field>
              <Field label="Display name">
                <input className="am-input" value={draft.display_name} onChange={(e) => setDraft({ ...draft, display_name: e.target.value })} autoComplete="off" />
              </Field>
              <Field label="First password" hint="They can change it in their profile.">
                <input className="am-input" type="password" value={draft.password} onChange={(e) => setDraft({ ...draft, password: e.target.value })} autoComplete="new-password" />
              </Field>
              <Field label="Role">
                <select className="am-input" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value as Role })}>
                  <option value="member">Member: builds and runs workflows</option>
                  <option value="admin">Admin: also manages people and settings</option>
                </select>
              </Field>
            </div>
          </Card>
        </form>
      )}

      <div className="overflow-hidden rounded-xl border border-slate-200">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2 font-semibold">Person</th>
              <th className="px-3 py-2 font-semibold">Role</th>
              <th className="px-3 py-2 font-semibold">Last sign-in</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {(users ?? []).map((u) => (
              <tr key={u.id} className={u.active ? "" : "opacity-60"}>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2.5">
                    <Avatar user={u} />
                    <div className="min-w-0">
                      <div className="truncate font-medium text-slate-900">
                        {u.display_name} {u.id === me?.id && <span className="text-xs font-normal text-slate-400">(you)</span>}
                      </div>
                      <div className="truncate text-xs text-slate-500">
                        @{u.username}
                        {!u.active && " · deactivated"}
                      </div>
                    </div>
                  </div>
                </td>
                <td className="px-3 py-2">
                  <select
                    className="am-input !w-28 !py-1 text-xs"
                    value={u.role}
                    aria-label={`Role of ${u.display_name}`}
                    onChange={(e) => void update(u, { role: e.target.value as Role }, `${u.display_name} is now ${e.target.value === "admin" ? "an admin" : "a member"}`)}
                  >
                    <option value="member">Member</option>
                    <option value="admin">Admin</option>
                  </select>
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">{fmtDate(u.last_login_at)}</td>
                <td className="px-3 py-2 text-right">
                  {resetFor === u.id ? (
                    <form
                      className="flex items-center justify-end gap-1.5"
                      onSubmit={(e) => {
                        e.preventDefault();
                        void update(u, { password: resetPw }, `Password reset for ${u.display_name}`).then((ok) => {
                          if (ok) {
                            setResetFor(null);
                            setResetPw("");
                          }
                        });
                      }}
                    >
                      <input className="am-input !w-36 !py-1 text-xs" type="password" placeholder="New password" value={resetPw} onChange={(e) => setResetPw(e.target.value)} autoComplete="new-password" autoFocus />
                      <button type="submit" className="am-btn !px-2 !py-1 text-xs" disabled={!resetPw}>
                        Set
                      </button>
                      <button type="button" className="am-btn !px-2 !py-1 text-xs" onClick={() => setResetFor(null)}>
                        Cancel
                      </button>
                    </form>
                  ) : (
                    <div className="flex justify-end gap-1.5">
                      <button type="button" className="am-btn !px-2 !py-1 text-xs" onClick={() => setResetFor(u.id)}>
                        Reset password
                      </button>
                      {u.id !== me?.id && (
                        <button
                          type="button"
                          className={`am-btn !px-2 !py-1 text-xs ${u.active ? "text-red-600" : ""}`}
                          onClick={() => void update(u, { active: !u.active }, u.active ? `${u.display_name} was deactivated and signed out` : `${u.display_name} can sign in again`)}
                        >
                          {u.active ? "Deactivate" : "Reactivate"}
                        </button>
                      )}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users === null && !error && <div className="p-3 text-sm text-slate-500">Loading…</div>}
      </div>
    </>
  );
}

// ---- Projects ---------------------------------------------------------------------------------
/** The exact word to type before a project can be deleted. */
export const DELETE_WORD = "DELETE";

function ProjectsSection({
  currentProjectId,
  onOpenProject,
  onProjectDeleted,
  notify,
}: {
  currentProjectId: string | null;
  onOpenProject: (id: string) => void;
  onProjectDeleted: (id: string) => Promise<void>;
  notify: Props["notify"];
}) {
  const { user, mode } = useAuth();
  const canDelete = mode === "off" || user?.role === "admin";
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [confirming, setConfirming] = useState<ProjectSummary | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.projects().then(
      (list) => setProjects([...list].sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))),
      (err) => setError(errorMessage(err)),
    );
  }, []);
  useEffect(load, [load]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (projects ?? []).filter((p) => !q || p.name.toLowerCase().includes(q) || p.description.toLowerCase().includes(q));
  }, [projects, query]);

  const startDelete = (p: ProjectSummary) => {
    setConfirming(p);
    setTyped("");
  };

  const confirmDelete = async () => {
    if (!confirming || typed !== DELETE_WORD) return;
    setBusy(true);
    try {
      await api.deleteProject(confirming.id);
      notify(`Deleted ${confirming.name}`, "success");
      const id = confirming.id;
      setConfirming(null);
      setTyped("");
      await onProjectDeleted(id);
      load();
    } catch (err) {
      notify(errorMessage(err), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <SectionHeader title="Projects">
        All saved projects in this workspace.{" "}
        {canDelete
          ? "Deleting a project also deletes its revisions, runs, traces, files, evaluations and vector indexes."
          : "Only admins can delete projects."}
      </SectionHeader>
      {error && (
        <div className="mb-3">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      <input
        className="am-input mb-3"
        placeholder={projects ? `Search ${projects.length} project${projects.length === 1 ? "" : "s"}…` : "Search…"}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        aria-label="Search projects"
      />
      <div className="overflow-hidden rounded-xl border border-slate-200">
        <ul className="divide-y divide-slate-100">
          {shown.map((p) => (
            <li key={p.id} className="px-3 py-2.5">
              <div className="flex items-center gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-slate-900">{p.name}</span>
                    {p.id === currentProjectId && (
                      <span className="shrink-0 rounded bg-accent-100 px-1.5 py-0.5 text-[10px] font-medium text-accent-700">open</span>
                    )}
                  </div>
                  <div className="truncate text-xs text-slate-500">
                    rev {p.revision} · updated {fmtDate(p.updated_at)}
                    {p.description && ` · ${p.description}`}
                  </div>
                </div>
                {p.id !== currentProjectId && (
                  <button type="button" className="am-btn !px-2 !py-1 text-xs" onClick={() => onOpenProject(p.id)}>
                    Open
                  </button>
                )}
                {canDelete && (
                  <button
                    type="button"
                    className="am-btn !px-2 !py-1 text-xs text-red-600"
                    onClick={() => startDelete(p)}
                    aria-label={`Delete ${p.name}`}
                  >
                    Delete
                  </button>
                )}
              </div>
              {confirming?.id === p.id && (
                <form
                  className="mt-2.5 rounded-lg border border-red-200 bg-red-50 p-3"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void confirmDelete();
                  }}
                >
                  <p className="text-xs text-red-800">
                    <strong>This can’t be undone.</strong> “{p.name}” and its {p.revision} revision{p.revision === 1 ? "" : "s"},
                    runs, traces, uploaded files, evaluations and vector indexes will be permanently deleted.
                  </p>
                  <label className="mt-2 block text-xs text-red-800" htmlFor={`confirm-${p.id}`}>
                    Type <span className="rounded bg-surface px-1 font-mono font-semibold">{DELETE_WORD}</span> to confirm
                  </label>
                  <div className="mt-1 flex gap-2">
                    <input
                      id={`confirm-${p.id}`}
                      className="am-input font-mono"
                      value={typed}
                      onChange={(e) => setTyped(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                      autoFocus
                      aria-describedby={`confirm-${p.id}`}
                    />
                    <button type="button" className="am-btn" onClick={() => setConfirming(null)} disabled={busy}>
                      Cancel
                    </button>
                    <button
                      type="submit"
                      className="inline-flex shrink-0 items-center rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-40"
                      disabled={typed !== DELETE_WORD || busy}
                    >
                      {busy ? "Deleting…" : "Delete forever"}
                    </button>
                  </div>
                </form>
              )}
            </li>
          ))}
          {projects !== null && shown.length === 0 && (
            <li className="px-3 py-4 text-center text-xs text-slate-500">{query ? "No projects match." : "No projects yet."}</li>
          )}
          {projects === null && !error && <li className="px-3 py-3 text-sm text-slate-500">Loading…</li>}
        </ul>
      </div>
    </>
  );
}

// ---- Appearance -------------------------------------------------------------------------------
function AppearanceSection({ theme, onTheme }: { theme: ThemePreference; onTheme: (t: ThemePreference) => void }) {
  const options: { id: ThemePreference; label: string; hint: string }[] = [
    { id: "light", label: "Light", hint: "Bright canvas" },
    { id: "dark", label: "Dark", hint: "Easy on the eyes" },
    { id: "system", label: "System", hint: "Follow this device" },
  ];
  return (
    <>
      <SectionHeader title="Appearance">Saved in this browser.</SectionHeader>
      <div className="grid grid-cols-3 gap-3" role="radiogroup" aria-label="Theme">
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={theme === o.id}
            onClick={() => onTheme(o.id)}
            className={`rounded-xl border p-3 text-left transition ${theme === o.id ? "border-accent-400 ring-2 ring-accent-500/20" : "border-slate-200 hover:border-slate-300"}`}
          >
            <div
              className={`mb-2 h-14 rounded-lg ${
                o.id === "light" ? "bg-gradient-to-br from-[#ffffff] to-[#e2e8f0]" : o.id === "dark" ? "bg-gradient-to-br from-[#334155] to-[#0b1020]" : "bg-gradient-to-r from-[#f1f5f9] via-[#94a3b8] to-[#0f172a]"
              }`}
            />
            <div className="text-sm font-medium text-slate-900">{o.label}</div>
            <div className="text-xs text-slate-500">{o.hint}</div>
          </button>
        ))}
      </div>
    </>
  );
}

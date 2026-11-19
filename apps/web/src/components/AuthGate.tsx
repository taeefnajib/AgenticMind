import { createContext, useCallback, useContext, useEffect, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, AUTH_REQUIRED_EVENT, auth, isConnectionError } from "../api/client";
import type { AuthStatus, User } from "../api/types";
import { useTheme } from "../lib/useTheme";

interface AuthValue {
  /** null when sign-in is off (AGENTICMIND_AUTH=off): one anonymous local user. */
  user: User | null;
  mode: AuthStatus["mode"];
  setUser: (user: User) => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthValue>({
  user: null,
  mode: "off",
  setUser: () => undefined,
  signOut: async () => undefined,
});

export const useAuth = () => useContext(AuthContext);

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { message?: string } | string | undefined;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
  }
  return (err as Error).message;
}

type State =
  | { kind: "loading" }
  | { kind: "unreachable"; message: string }
  | { kind: "signed-out"; setup: boolean; notice?: string }
  | { kind: "ready"; status: AuthStatus };

/** Shows sign-in (or first-run setup) until there is a session, then the app. */
export function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<State>({ kind: "loading" });

  const load = useCallback(async (notice?: string) => {
    try {
      const status = await auth.status();
      if (status.mode === "off" || status.user) setState({ kind: "ready", status });
      else setState({ kind: "signed-out", setup: status.setup_required, notice });
    } catch (err) {
      setState({ kind: "unreachable", message: errorMessage(err) });
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // A request answered 401: the session expired or was signed out elsewhere.
  useEffect(() => {
    const onExpired = () =>
      setState((s) =>
        s.kind === "ready" && s.status.mode === "required"
          ? { kind: "signed-out", setup: false, notice: "Your session ended. Sign in again to continue." }
          : s,
      );
    window.addEventListener(AUTH_REQUIRED_EVENT, onExpired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onExpired);
  }, []);

  const signOut = useCallback(async () => {
    try {
      await auth.logout();
    } finally {
      setState({ kind: "signed-out", setup: false, notice: "You are signed out." });
    }
  }, []);

  if (state.kind === "loading") return <Splash />;
  if (state.kind === "unreachable") {
    return (
      <AuthShell>
        <h1 className="text-lg font-semibold text-slate-900">Can’t reach the AgenticMind API</h1>
        <p className="mt-2 text-sm text-slate-600">
          {state.message}. Make sure <code className="rounded bg-slate-100 px-1 font-mono text-xs">npm run dev:api</code>{" "}
          is running.
        </p>
        <button type="button" className="am-btn-primary mt-5 w-full justify-center" onClick={() => void load()}>
          Try again
        </button>
      </AuthShell>
    );
  }
  if (state.kind === "signed-out") {
    return (
      <SignIn
        setup={state.setup}
        notice={state.notice}
        onSignedIn={(user) => setState({ kind: "ready", status: { mode: "required", setup_required: false, user } })}
        onSetupDone={() => void load()}
      />
    );
  }
  const { status } = state;
  const value: AuthValue = {
    user: status.user,
    mode: status.mode,
    setUser: (user) => setState({ kind: "ready", status: { ...status, user } }),
    signOut,
  };
  // keyed by user: signing in as someone else starts the workspace fresh
  return (
    <AuthContext.Provider value={value}>
      <div key={status.user?.id ?? "local"} className="h-full">
        {children}
      </div>
    </AuthContext.Provider>
  );
}

function Splash() {
  return (
    <div className="flex h-full items-center justify-center bg-slate-100" aria-busy="true">
      <img src="/logo.svg" alt="AgenticMind" className="h-10 w-auto animate-pulse" />
    </div>
  );
}

function AuthShell({ children }: { children: ReactNode }) {
  useTheme(); // applies the saved light/dark preference before the app has loaded
  return (
    <div className="am-auth-bg relative bg-slate-100 flex min-h-full items-center justify-center overflow-auto px-4 py-10">
      <div className="relative w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center text-center">
          <img src="/logo.svg" alt="" className="h-12 w-auto" />
          <div className="mt-3 text-xl font-semibold tracking-tight text-slate-900">AgenticMind</div>
          <div className="text-xs text-slate-500">Visual AI engineering platform</div>
        </div>
        <div className="rounded-2xl border border-slate-200 bg-surface p-6 shadow-xl shadow-slate-900/5">{children}</div>
      </div>
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-700">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] leading-snug text-slate-500">{hint}</span>}
    </label>
  );
}

function SignIn({
  setup,
  notice,
  onSignedIn,
  onSetupDone,
}: {
  setup: boolean;
  notice?: string;
  onSignedIn: (user: User) => void;
  onSetupDone: () => void;
}) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (setup && password !== confirm) {
      setError("The two passwords don’t match.");
      return;
    }
    setBusy(true);
    try {
      if (setup) {
        await auth.setup({ username, display_name: displayName || username, password, email: email || null });
        onSetupDone();
      } else {
        onSignedIn(await auth.login(username, password));
      }
    } catch (err) {
      setError(isConnectionError(err) ? "The API is not reachable." : errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <AuthShell>
      <form onSubmit={(e) => void submit(e)} className="space-y-3.5" noValidate>
        <div>
          <h1 className="text-lg font-semibold text-slate-900">{setup ? "Create the admin account" : "Sign in"}</h1>
          <p className="mt-1 text-xs text-slate-500">
            {setup
              ? "This workspace has no accounts yet. The first one is its admin: it can add people and change workspace settings."
              : "Welcome back. Sign in to open your workflows."}
          </p>
        </div>
        {notice && !error && (
          <div className="rounded-md bg-accent-100 px-3 py-2 text-xs text-accent-700" role="status">
            {notice}
          </div>
        )}
        {error && (
          <div className="rounded-md bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
            {error}
          </div>
        )}
        <Field label="Username" hint={setup ? "Lowercase letters, digits, '.', '_' or '-'." : undefined}>
          <input
            className="am-input"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
        </Field>
        {setup && (
          <>
            <Field label="Display name">
              <input
                className="am-input"
                autoComplete="name"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="How your name appears"
              />
            </Field>
            <Field label="Email (optional)">
              <input
                className="am-input"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
          </>
        )}
        <Field label="Password" hint={setup ? "At least 8 characters. A short phrase works well." : undefined}>
          <input
            className="am-input"
            type="password"
            autoComplete={setup ? "new-password" : "current-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </Field>
        {setup && (
          <Field label="Confirm password">
            <input
              className="am-input"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              required
            />
          </Field>
        )}
        <button
          type="submit"
          className="am-brand-gradient w-full rounded-md px-3.5 py-2 text-sm font-medium text-white shadow-md shadow-fuchsia-500/20 transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-60"
          disabled={busy || !username || !password}
        >
          {busy ? "One moment…" : setup ? "Create account and continue" : "Sign in"}
        </button>
        {!setup && (
          <p className="text-center text-[11px] text-slate-500">Need an account or a new password? Ask a workspace admin.</p>
        )}
      </form>
    </AuthShell>
  );
}

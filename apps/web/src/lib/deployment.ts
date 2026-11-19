import type { DeployCheck, Deployment, DeploymentStatus, DeployMode } from "../api/types";

const ENV_KEY = /^[A-Z][A-Z0-9_]{0,63}$/;

/** Parse "KEY=value" lines (blank lines and # comments ignored). Mirrors the server's name rule. */
export function parseEnv(text: string): { env: Record<string, string>; errors: string[] } {
  const env: Record<string, string> = {};
  const errors: string[] = [];
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line || line.startsWith("#")) return;
    const eq = line.indexOf("=");
    if (eq <= 0) {
      errors.push(`Line ${i + 1}: use KEY=value`);
      return;
    }
    const key = line.slice(0, eq).trim();
    if (!ENV_KEY.test(key)) {
      errors.push(`Line ${i + 1}: names look like MY_SETTING`);
      return;
    }
    env[key] = line.slice(eq + 1).trim();
  });
  return { env, errors };
}

export function formatEnv(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

export const MODE_LABEL: Record<DeployMode, string> = {
  direct: "Deploys here",
  guided: "Generates files",
  plan: "Plan only",
};

export const STATUS_LABEL: Record<DeploymentStatus, string> = {
  deploying: "Deploying…",
  running: "Running",
  live: "Live",
  failed: "Failed",
  superseded: "Superseded",
  stopped: "Stopped",
  artifacts_ready: "Files ready",
};

export function statusTone(status: DeploymentStatus): "ok" | "busy" | "bad" | "idle" {
  if (status === "running" || status === "live") return "ok";
  if (status === "deploying") return "busy";
  if (status === "failed") return "bad";
  return "idle";
}

export function bySeverity(checks: DeployCheck[]): DeployCheck[] {
  const rank = { blocker: 0, warning: 1, info: 2 } as const;
  return [...checks].sort((a, b) => rank[a.severity] - rank[b.severity]);
}

/** An earlier deployment you can return to: it ran successfully once and is not the current one. */
export function canRollBackTo(dep: Deployment, history: Deployment[]): boolean {
  if (dep.status !== "superseded" && dep.status !== "stopped") return false;
  if (dep.mode === "plan") return false;
  const current = history.find((d) => d.target === dep.target && (d.status === "running" || d.status === "live"));
  return !current || current.revision !== dep.revision || current.profile !== dep.profile;
}

export function isActive(history: Deployment[]): boolean {
  return history.some((d) => d.status === "deploying");
}

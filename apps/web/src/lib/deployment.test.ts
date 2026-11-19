import { describe, expect, it } from "vitest";

import type { Deployment } from "../api/types";
import { bySeverity, canRollBackTo, formatEnv, parseEnv, statusTone } from "./deployment";

const dep = (over: Partial<Deployment>): Deployment => ({
  id: "d",
  project_id: "p",
  revision: 1,
  target: "local",
  target_name: "Local Docker",
  profile: "development",
  mode: "direct",
  status: "running",
  plan_hash: "h",
  endpoint: "",
  health: null,
  env: {},
  image: "",
  rollback_of: null,
  error: "",
  checks: [],
  secrets: [],
  created_at: "",
  finished_at: null,
  stopped_at: null,
  ...over,
});

describe("deployment helpers", () => {
  it("parses settings and rejects bad names like the server", () => {
    const { env, errors } = parseEnv("# comment\nLOG_LEVEL=info\n\nURL=https://x?a=b\nlower=1\nnovalue");
    expect(env).toEqual({ LOG_LEVEL: "info", URL: "https://x?a=b" });
    expect(errors).toEqual(["Line 5: names look like MY_SETTING", "Line 6: use KEY=value"]);
    expect(parseEnv(formatEnv(env)).env).toEqual(env);
  });

  it("offers rollback only to an earlier successful deployment", () => {
    const old = dep({ id: "a", revision: 1, status: "superseded" });
    const cur = dep({ id: "b", revision: 2, status: "running" });
    expect(canRollBackTo(old, [cur, old])).toBe(true);
    expect(canRollBackTo(cur, [cur, old])).toBe(false);
    expect(canRollBackTo(dep({ status: "failed" }), [cur])).toBe(false);
    expect(canRollBackTo(dep({ id: "c", revision: 2, status: "superseded" }), [cur])).toBe(false); // same as current
  });

  it("orders blockers first and maps status tones", () => {
    const sorted = bySeverity([
      { id: "i", severity: "info", title: "", detail: "", remedy: "" },
      { id: "b", severity: "blocker", title: "", detail: "", remedy: "" },
      { id: "w", severity: "warning", title: "", detail: "", remedy: "" },
    ]);
    expect(sorted.map((c) => c.id)).toEqual(["b", "w", "i"]);
    expect(statusTone("deploying")).toBe("busy");
    expect(statusTone("live")).toBe("ok");
  });
});

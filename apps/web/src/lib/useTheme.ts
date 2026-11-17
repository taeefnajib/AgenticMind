import { useEffect, useState } from "react";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const KEY = "am.theme";

function readPreference(): ThemePreference {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" || v === "system" ? v : "system";
  } catch {
    return "system";
  }
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function resolveTheme(pref: ThemePreference, system: ResolvedTheme): ResolvedTheme {
  return pref === "system" ? system : pref;
}

export const NEXT_THEME: Record<ThemePreference, ThemePreference> = { light: "dark", dark: "system", system: "light" };

/** Theme preference (persisted) + the resolved theme, kept in sync with the `dark` class on <html>. */
export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(readPreference);
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme);
  const resolved = resolveTheme(preference, system);

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = () => setSystem(mq.matches ? "dark" : "light");
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark");
  }, [resolved]);

  const update = (next: ThemePreference) => {
    setPreference(next);
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      /* storage unavailable: preference lasts for this session only */
    }
  };

  return { preference, resolved, setPreference: update };
}

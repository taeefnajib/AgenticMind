import { useEffect, useRef, useState } from "react";

import type { User } from "../api/types";
import { useAuth } from "./AuthGate";

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const first = parts[0] ?? "";
  const last = parts.length > 1 ? (parts[parts.length - 1] ?? "") : "";
  if (!first) return "?";
  return (last ? first.charAt(0) + last.charAt(0) : first.slice(0, 2)).toUpperCase();
}

export function Avatar({ user, size = "sm" }: { user: User | null; size?: "sm" | "lg" }) {
  const cls = size === "lg" ? "h-14 w-14 text-lg" : "h-7 w-7 text-[11px]";
  return (
    <span
      className={`am-brand-gradient inline-flex shrink-0 select-none items-center justify-center rounded-full font-semibold text-white ${cls}`}
      aria-hidden="true"
    >
      {user ? initials(user.display_name) : (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
          <circle cx="12" cy="8" r="4" />
          <path d="M4 21a8 8 0 0 1 16 0" />
        </svg>
      )}
    </span>
  );
}

/** Avatar in the toolbar: who is signed in, Profile, Settings, Sign out. */
export function UserMenu({ onOpen }: { onOpen: (section: "profile" | "projects" | "assistant") => void }) {
  const { user, mode, signOut } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const pick = (section: "profile" | "projects" | "assistant") => {
    setOpen(false);
    onOpen(section);
  };

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        className="flex items-center rounded-full p-0.5 transition hover:ring-2 hover:ring-accent-500/30"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={user ? `Account: ${user.display_name}` : "Account and settings"}
        title={user ? `${user.display_name} (@${user.username})` : "Settings"}
      >
        <Avatar user={user} />
      </button>
      {open && (
        <div
          className="absolute right-0 top-full z-50 mt-2 w-60 overflow-hidden rounded-xl border border-slate-200 bg-surface shadow-xl"
          role="menu"
        >
          <div className="flex items-center gap-3 border-b border-slate-100 px-3 py-3">
            <Avatar user={user} />
            <div className="min-w-0">
              <div className="truncate text-sm font-medium text-slate-900">{user?.display_name ?? "Local user"}</div>
              <div className="truncate text-xs text-slate-500">
                {user ? `@${user.username} · ${user.role === "admin" ? "Admin" : "Member"}` : "Sign-in is off"}
              </div>
            </div>
          </div>
          <div className="py-1 text-sm">
            <MenuItem onClick={() => pick("profile")}>{mode === "off" ? "Account" : "Profile"}</MenuItem>
            <MenuItem onClick={() => pick("projects")}>My projects</MenuItem>
            <MenuItem onClick={() => pick("assistant")}>Settings</MenuItem>
            {mode === "required" && (
              <MenuItem
                onClick={() => {
                  setOpen(false);
                  void signOut();
                }}
              >
                Sign out
              </MenuItem>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function MenuItem({ onClick, children }: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      role="menuitem"
      className="block w-full px-3 py-1.5 text-left text-slate-700 hover:bg-slate-50 hover:text-slate-900"
      onClick={onClick}
    >
      {children}
    </button>
  );
}

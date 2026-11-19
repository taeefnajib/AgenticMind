import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import type { AssistantResult, AssistantStatus, AssistantTurn, ProjectDocument, ProjectOut } from "../api/types";
import {
  describeChanges,
  shortType,
  splitDiagnostics,
  SUGGESTIONS_EMPTY,
  SUGGESTIONS_EXISTING,
} from "../lib/assistant";

interface Props {
  projectId: string | null;
  projectName: string;
  /** The graph is empty (a blank project): suggestions are about creating a workflow. */
  empty: boolean;
  disabled?: boolean;
  /** The current (possibly unsaved) canvas graph. */
  getDocument: () => ProjectDocument | null;
  /** Save this document as the project's new revision and load it onto the canvas. */
  onApply: (doc: ProjectDocument) => Promise<ProjectOut | null>;
  onNewBlankProject: () => Promise<void>;
}

type Phase = "closed" | "flapping" | "open" | "closing";

let nextId = 1;

interface UserMsg {
  id: number;
  role: "user";
  text: string;
}
interface AssistantMsg {
  id: number;
  role: "assistant";
  text: string;
  result?: AssistantResult;
  error?: string;
  applied?: { revision: number; previous: ProjectDocument };
  undone?: boolean;
}
type Msg = UserMsg | AssistantMsg;

const FLAP_MS = 720;
const CLOSE_MS = 200;

const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** The AgenticMind wings mark. `flap` plays one flap; `fly` keeps flapping (while thinking). */
export function WingsIcon({ className = "h-7 w-7", state }: { className?: string; state?: "flap" | "fly" }) {
  const wing = "M21 23 C16 14 8 11.5 2.5 13.5 C7.5 16 9.5 18.5 11.5 20.5 C7.5 20.8 4.8 22.8 3.8 26 C8.5 25.8 12.5 26 15.5 27 C12.8 29 11 31.4 10.8 34.6 C15 32.6 18.8 29.6 21 26.5 Z";
  return (
    <svg viewBox="0 0 48 48" className={`${className} ${state === "flap" ? "am-flap" : state === "fly" ? "am-fly" : ""}`} aria-hidden="true">
      <g className="am-wing am-wing-l">
        <path d={wing} fill="currentColor" opacity="0.92" />
      </g>
      <g className="am-wing am-wing-r">
        <path d={wing} fill="currentColor" opacity="0.92" transform="matrix(-1 0 0 1 48 0)" />
      </g>
      <ellipse cx="24" cy="26" rx="2.6" ry="6.2" fill="currentColor" />
      <circle cx="24" cy="16.6" r="2.6" fill="currentColor" />
    </svg>
  );
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { message?: string } | string | undefined;
    if (typeof d === "string") return d;
    if (d?.message) return d.message;
  }
  return (err as Error).message;
}

export function AiAssistant({ projectId, projectName, empty, disabled, getDocument, onApply, onNewBlankProject }: Props) {
  const [phase, setPhase] = useState<Phase>("closed");
  const [threads, setThreads] = useState<Record<string, Msg[]>>({});
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const messages = (projectId && threads[projectId]) || [];

  const open = useCallback(() => {
    if (phase !== "closed") return;
    if (reducedMotion()) {
      setPhase("open");
      return;
    }
    setPhase("flapping");
    window.setTimeout(() => setPhase("open"), FLAP_MS);
  }, [phase]);

  const close = useCallback(() => {
    if (reducedMotion()) {
      setPhase("closed");
      return;
    }
    setPhase("closing");
    window.setTimeout(() => setPhase("closed"), CLOSE_MS);
  }, []);

  useEffect(() => {
    if (phase !== "open") return;
    api.assistantStatus().then(setStatus, () => setStatus(null));
    window.setTimeout(() => inputRef.current?.focus(), 60);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase, close]);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, busy]);

  const update = useCallback(
    (pid: string, fn: (msgs: Msg[]) => Msg[]) => setThreads((t) => ({ ...t, [pid]: fn(t[pid] ?? []) })),
    [],
  );

  const apply = useCallback(
    async (pid: string, id: number, doc: ProjectDocument) => {
      const previous = getDocument();
      const saved = await onApply(doc);
      if (saved && previous) {
        update(pid, (msgs) =>
          msgs.map((m) => (m.id === id && m.role === "assistant" ? { ...m, applied: { revision: saved.revision, previous }, undone: false } : m)),
        );
      }
    },
    [getDocument, onApply, update],
  );

  const undo = useCallback(
    async (pid: string, id: number) => {
      const msg = (threads[pid] ?? []).find((m) => m.id === id);
      if (!msg || msg.role !== "assistant" || !msg.applied) return;
      const saved = await onApply(msg.applied.previous);
      if (saved) update(pid, (msgs) => msgs.map((m) => (m.id === id ? { ...m, undone: true } : m)));
    },
    [threads, onApply, update],
  );

  const send = useCallback(
    async (text: string) => {
      const message = text.trim();
      if (!message || !projectId || busy) return;
      const pid = projectId;
      const history: AssistantTurn[] = messages
        .filter((m) => m.text && !(m.role === "assistant" && m.error))
        .map((m) => ({ role: m.role, content: m.text }));
      update(pid, (msgs) => [...msgs, { id: nextId++, role: "user", text: message }]);
      setInput("");
      setBusy(true);
      try {
        const document = getDocument() ?? undefined;
        const result = await api.designWorkflow(pid, { message, history, document });
        const id = nextId++;
        update(pid, (msgs) => [...msgs, { id, role: "assistant", text: result.reply, result }]);
        if (result.document && result.applicable) await apply(pid, id, result.document);
      } catch (err) {
        update(pid, (msgs) => [...msgs, { id: nextId++, role: "assistant", text: "", error: errorText(err) }]);
      } finally {
        setBusy(false);
        window.setTimeout(() => inputRef.current?.focus(), 30);
      }
    },
    [projectId, busy, messages, update, getDocument, apply],
  );

  const suggestions = empty ? SUGGESTIONS_EMPTY : SUGGESTIONS_EXISTING;
  const showDialog = phase === "open" || phase === "closing";

  return (
    <>
      <div className="pointer-events-none absolute inset-x-0 bottom-4 z-10 flex justify-center">
        <button
          type="button"
          onClick={open}
          disabled={disabled || !projectId}
          aria-label="Design with AI"
          aria-expanded={showDialog}
          title="Design with AI"
          className={`am-launcher group pointer-events-auto flex h-14 w-14 items-center justify-center rounded-full bg-gradient-to-br from-accent-500 to-accent-700 text-white shadow-lg ring-4 ring-accent-500/15 transition hover:scale-105 focus:outline-none focus-visible:ring-accent-400/60 disabled:cursor-not-allowed disabled:opacity-50 ${
            phase === "flapping" ? "am-launch" : ""
          } ${showDialog ? "invisible" : ""}`}
        >
          <WingsIcon className="h-8 w-8" state={phase === "flapping" ? "flap" : undefined} />
          <span className="pointer-events-none absolute bottom-full mb-2 whitespace-nowrap rounded-md bg-slate-900 px-2 py-1 text-xs font-medium text-slate-50 opacity-0 shadow transition group-hover:opacity-100">
            Design with AI
          </span>
        </button>
      </div>

      {showDialog && (
        <div
          className={`fixed inset-0 z-50 flex items-end justify-center bg-slate-900/25 p-4 backdrop-blur-md sm:items-center ${
            phase === "closing" ? "am-backdrop-out" : "am-backdrop-in"
          }`}
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) close();
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="ai-title"
            className={`flex h-[min(720px,calc(100vh-2rem))] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-surface shadow-2xl ${
              phase === "closing" ? "am-pop-out" : "am-pop-in"
            }`}
          >
            <header className="flex items-center gap-3 border-b border-slate-200 px-5 py-3.5">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent-500 to-accent-700 text-white">
                <WingsIcon className="h-6 w-6" state={busy ? "fly" : undefined} />
              </span>
              <div className="min-w-0 flex-1">
                <h2 id="ai-title" className="text-sm font-semibold text-slate-900">
                  AI workflow designer
                </h2>
                <p className="truncate text-xs text-slate-500">
                  Changes apply to <span className="font-medium text-slate-700">{projectName || "this project"}</span>
                  {status && <> · {status.model}</>}
                </p>
              </div>
              <button type="button" className="am-btn" onClick={() => void onNewBlankProject()} disabled={busy} title="Create an empty project and design it here">
                New blank project
              </button>
              <button type="button" className="rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-800" onClick={close} aria-label="Close">
                <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden="true">
                  <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
                </svg>
              </button>
            </header>

            {status && !status.configured && (
              <div className="border-b border-amber-200 bg-amber-50 px-5 py-2 text-xs text-amber-800">
                The assistant needs the <code className="font-mono">{status.secret}</code> secret. Add it to <code className="font-mono">.env</code> and restart the API.
              </div>
            )}

            <div ref={listRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4" aria-live="polite">
              {messages.length === 0 && (
                <div className="mx-auto max-w-md pt-6 text-center">
                  <p className="text-sm text-slate-600">
                    {empty
                      ? "Describe the workflow you want. I’ll build it on the canvas, check it, and save it as a new revision you can undo."
                      : "Ask for a change to this workflow, or a question about it. Every change is checked, saved as a revision, and can be undone."}
                  </p>
                  <div className="mt-5 flex flex-col gap-2">
                    {suggestions.map((s) => (
                      <button
                        key={s}
                        type="button"
                        className="rounded-lg border border-slate-200 px-3 py-2 text-left text-sm text-slate-700 transition hover:border-accent-300 hover:bg-accent-50 hover:text-accent-800"
                        onClick={() => void send(s)}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {messages.map((m) =>
                m.role === "user" ? (
                  <div key={m.id} className="flex justify-end">
                    <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent-600 px-3.5 py-2 text-sm text-white">{m.text}</div>
                  </div>
                ) : (
                  <AssistantBubble
                    key={m.id}
                    msg={m}
                    projectName={projectName}
                    onApply={(doc) => projectId && void apply(projectId, m.id, doc)}
                    onUndo={() => projectId && void undo(projectId, m.id)}
                    busy={busy}
                  />
                ),
              )}
              {busy && (
                <div className="flex items-center gap-2 text-sm text-slate-500">
                  <WingsIcon className="h-5 w-5 text-accent-600" state="fly" />
                  Designing and checking the graph…
                </div>
              )}
            </div>

            <form
              className="flex items-end gap-2 border-t border-slate-200 px-4 py-3"
              onSubmit={(e) => {
                e.preventDefault();
                void send(input);
              }}
            >
              <textarea
                ref={inputRef}
                className="am-input max-h-40 min-h-[2.5rem] resize-none"
                rows={2}
                placeholder={empty ? "e.g. Answer any question with yes or no" : "e.g. Add a step that translates the answer"}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    void send(input);
                  }
                }}
                disabled={busy}
                aria-label="Message the AI workflow designer"
              />
              <button type="submit" className="am-btn-primary" disabled={busy || !input.trim()}>
                Send
              </button>
            </form>
          </div>
        </div>
      )}
    </>
  );
}

function AssistantBubble({
  msg,
  projectName,
  onApply,
  onUndo,
  busy,
}: {
  msg: AssistantMsg;
  projectName: string;
  onApply: (doc: ProjectDocument) => void;
  onUndo: () => void;
  busy: boolean;
}) {
  if (msg.error) {
    return (
      <div className="max-w-[90%] rounded-2xl rounded-bl-md border border-red-200 bg-red-50 px-3.5 py-2 text-sm text-red-800" role="alert">
        {msg.error}
      </div>
    );
  }
  const r = msg.result;
  const { blocking, notes } = splitDiagnostics(r?.diagnostics ?? []);
  const nodesTouched = [...(r?.changes.added ?? []).map((n) => ["+", n] as const), ...(r?.changes.modified ?? []).map((n) => ["~", n] as const), ...(r?.changes.removed ?? []).map((n) => ["−", n] as const)];
  return (
    <div className="max-w-[90%] space-y-2">
      <div className="whitespace-pre-wrap rounded-2xl rounded-bl-md bg-slate-100 px-3.5 py-2 text-sm text-slate-800">{msg.text || "(no reply)"}</div>
      {r?.document && (
        <div className="rounded-xl border border-slate-200 px-3.5 py-2.5 text-xs">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-slate-700">{describeChanges(r.changes)}</span>
            <span className="text-slate-400">
              {r.attempts > 1 ? `${r.attempts} attempts · ` : ""}
              {r.model}
            </span>
          </div>
          {nodesTouched.length > 0 && (
            <ul className="mt-1.5 flex flex-wrap gap-1.5">
              {nodesTouched.map(([sign, n]) => (
                <li
                  key={sign + n.id}
                  className={`rounded px-1.5 py-0.5 font-mono ${
                    sign === "+" ? "bg-emerald-100 text-emerald-800" : sign === "~" ? "bg-sky-100 text-sky-800" : "bg-red-100 text-red-800"
                  }`}
                  title={n.type}
                >
                  {sign} {n.id} <span className="opacity-60">{shortType(n.type)}</span>
                </li>
              ))}
            </ul>
          )}
          {blocking.length > 0 && (
            <div className="mt-2 rounded-md bg-red-50 px-2.5 py-2 text-red-800">
              <div className="font-medium">The proposal still has {blocking.length} problem(s), so it was not applied:</div>
              <ul className="mt-1 list-disc pl-4">
                {blocking.slice(0, 5).map((d, i) => (
                  <li key={i}>
                    <span className="font-mono">{d.code}</span> {d.message}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {notes.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-amber-700">
              {notes.slice(0, 4).map((d, i) => (
                <li key={i}>Note: {d.message}</li>
              ))}
            </ul>
          )}
          <div className="mt-2 flex items-center gap-2">
            {msg.applied && !msg.undone && (
              <>
                <span className="text-emerald-700">
                  ✓ Applied to {projectName || "the project"} as revision {msg.applied.revision}
                </span>
                <button type="button" className="am-btn ml-auto !px-2 !py-0.5 text-xs" onClick={onUndo} disabled={busy}>
                  Undo
                </button>
              </>
            )}
            {msg.undone && <span className="text-slate-500">Undone: the previous graph was restored as a new revision.</span>}
            {!msg.applied && !msg.undone && (
              <button type="button" className="am-btn !px-2 !py-0.5 text-xs" onClick={() => onApply(r.document!)} disabled={busy}>
                {blocking.length ? "Apply anyway as a draft" : "Apply"}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

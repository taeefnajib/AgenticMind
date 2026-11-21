import { useEffect, useMemo, useRef, useState } from "react";

import type { IRNode, JsonValue, RunOut, TraceEvent } from "../api/types";
import { Markdown } from "../lib/markdown";
import { useWorkbench } from "../lib/workbench";
import { IMAGE_ACCEPT, ImageThumb } from "./ImagePicker";

export interface ChatOutputOption {
  id: string;
  label: string;
  key: string;
}

export interface ChatResult {
  run: RunOut;
  events: TraceEvent[];
}

interface Citation {
  n: number;
  filename?: string | null;
  page?: number | null;
  section?: string | null;
  uri?: string;
  excerpt?: string;
  score?: number | null;
}

export interface AgentStep {
  tool: string;
  arguments?: Record<string, unknown>;
  ok: boolean;
  error?: string;
  result_summary?: string;
  duration_ms?: number;
  turn?: number;
  /** Steps taken by a subagent this call delegated to (ADR-0014). */
  substeps?: AgentStep[];
  /** ReAct: the model's reasoning before this action (ADR-0029). */
  thought?: string;
  subreport?: { stop_reason?: string; turns?: number };
}

interface Message {
  role: "user" | "assistant";
  text: string;
  /** File ID of an image attached to a user message. */
  image?: { id: string; name: string };
  citations?: Citation[];
  steps?: AgentStep[];
  stopReason?: string;
  invalid?: number[];
  sql?: { query: string; columns: string[]; rows: Record<string, unknown>[]; total: number; truncated: boolean };
  error?: string;
  meta?: { ms?: number; tokens?: number; cost?: number };
}

interface Props {
  inputNodes: IRNode[];
  /** Image Input nodes: the chat can attach an image to the first one. */
  imageInputs?: IRNode[];
  /** Run inputs set in the Run inputs tab (e.g. a fixed image), used unless the chat attaches one. */
  runInputs?: Record<string, JsonValue>;
  outputs: ChatOutputOption[];
  disabled: boolean;
  onAsk: (inputs: Record<string, JsonValue>, target: string) => Promise<ChatResult | null>;
}

type AnswerObject = {
  answer: string;
  citations?: Citation[];
  validation?: { invalid?: number[] };
  steps?: AgentStep[];
  report?: { stop_reason?: string };
  sql?: string;
  columns?: string[];
  rows?: Record<string, unknown>[];
  row_count?: number;
  truncated?: boolean;
};

/** Citation Builder results ({answer, citations}) and Agent results ({answer, steps, report}). */
function isAnswerObject(v: unknown): v is AnswerObject {
  return typeof v === "object" && v !== null && typeof (v as { answer?: unknown }).answer === "string";
}

export function toMessage(result: ChatResult, outputKey: string): Message {
  const { run, events } = result;
  let tokens = 0;
  let cost = 0;
  for (const e of events) {
    if (e.usage) {
      tokens += e.usage.input_tokens + e.usage.output_tokens;
      cost += e.usage.estimated_cost_usd ?? 0;
    }
  }
  const end = [...events].reverse().find((e) => e.event_type.startsWith("run_") && e.event_type !== "run_started");
  const meta = { ms: end?.duration_ms, tokens, cost };
  if (run.status === "waiting") {
    return { role: "assistant", text: "Paused for approval. Answer it in the **Approvals** tab to continue.", meta };
  }
  if (run.status !== "succeeded") {
    return { role: "assistant", text: "", error: run.error ? `${run.error.code}: ${run.error.message}` : `Run ${run.status}`, meta };
  }
  const value = run.outputs?.[outputKey];
  if (isAnswerObject(value)) {
    return {
      role: "assistant",
      text: value.answer,
      citations: value.citations,
      invalid: value.validation?.invalid,
      steps: value.steps,
      stopReason: value.report?.stop_reason,
      sql:
        typeof value.sql === "string"
          ? {
              query: value.sql,
              columns: value.columns ?? [],
              rows: value.rows ?? [],
              total: value.row_count ?? value.rows?.length ?? 0,
              truncated: Boolean(value.truncated),
            }
          : undefined,
      meta,
    };
  }
  if (typeof value === "string") return { role: "assistant", text: value, meta };
  if (value === undefined) return { role: "assistant", text: "", error: `The run produced no '${outputKey}' output`, meta };
  return { role: "assistant", text: JSON.stringify(value, null, 2), meta };
}

function sourceLabel(c: Citation): string {
  const parts = [c.filename ?? c.uri ?? `source ${c.n}`];
  if (c.page) parts.push(`p. ${c.page}`);
  if (c.section) parts.push(c.section);
  return parts.join(" · ");
}

function AnswerText({ message, onCite }: { message: Message; onCite: (n: number) => void }) {
  const byN = new Map((message.citations ?? []).map((c) => [c.n, c]));
  const cite = (n: number, key: string) => {
    const c = byN.get(n);
    return (
      <button
        key={key}
        type="button"
        onClick={() => onCite(n)}
        title={c ? `${sourceLabel(c)}\n\n${c.excerpt ?? ""}` : "Unknown source"}
        className={`mx-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded px-1 align-[1px] text-[10px] font-semibold ${
          c ? "bg-accent-100 text-accent-700 hover:bg-accent-200" : "bg-red-100 text-red-700"
        }`}
      >
        {n}
      </button>
    );
  };
  return <Markdown text={message.text} cite={cite} />;
}

/** Run-input keys with a fixed meaning for chat (ADR-0012): never offered as the question field. */
export const CONVERSATION_KEY = "conversation_id";
export const CONSENT_KEY = "memory_consent";

export function newConversationId(): string {
  return `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Inputs for one chat turn: the question, plus conversation id / consent when the graph reads them. */
export function chatInputs(
  inputNodes: IRNode[],
  questionKey: string,
  question: string,
  conversationId: string,
  consent: boolean,
): Record<string, JsonValue> {
  const keys = new Set(inputNodes.map((n) => n.config.key));
  const inputs: Record<string, JsonValue> = { [questionKey]: question };
  if (keys.has(CONVERSATION_KEY)) inputs[CONVERSATION_KEY] = conversationId;
  if (keys.has(CONSENT_KEY)) inputs[CONSENT_KEY] = consent;
  return inputs;
}

export function ChatPanel({ inputNodes, imageInputs = [], runInputs = {}, outputs, disabled, onAsk }: Props) {
  const { files } = useWorkbench();
  const imageKey = imageInputs[0] ? String(imageInputs[0].config.key ?? "image") : undefined;
  const [attached, setAttached] = useState<{ id: string; name: string } | null>(null);
  const [attaching, setAttaching] = useState(false);
  const attachInput = useRef<HTMLInputElement>(null);
  const attach = async (picked: File[]) => {
    if (!picked.length) return;
    setAttaching(true);
    try {
      const [added] = await files.upload(picked);
      if (added) setAttached({ id: added.id, name: added.filename });
    } finally {
      setAttaching(false);
    }
  };
  const textInputs = useMemo(
    () =>
      inputNodes.filter(
        (n) => (n.config.value_type ?? "String") === "String" && n.config.key !== CONVERSATION_KEY,
      ),
    [inputNodes],
  );
  const remembersConversation = inputNodes.some((n) => n.config.key === CONVERSATION_KEY);
  const asksConsent = inputNodes.some((n) => n.config.key === CONSENT_KEY);
  const [conversationId, setConversationId] = useState(newConversationId);
  const [consent, setConsent] = useState(false);
  const defaultInput = (textInputs.find((n) => n.config.key === "question") ?? textInputs[0])?.config.key as string | undefined;
  const defaultOutput = (outputs.find((o) => o.key === "answer") ?? outputs[0])?.id;
  const [inputKey, setInputKey] = useState<string | undefined>(defaultInput);
  const [target, setTarget] = useState<string | undefined>(defaultOutput);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [activeCite, setActiveCite] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!inputKey || !textInputs.some((n) => n.config.key === inputKey)) setInputKey(defaultInput);
  }, [defaultInput, inputKey, textInputs]);
  useEffect(() => {
    if (!target || !outputs.some((o) => o.id === target)) setTarget(defaultOutput);
  }, [defaultOutput, outputs, target]);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  const output = outputs.find((o) => o.id === target);
  const ready = Boolean(inputKey && output && !disabled);

  const send = async () => {
    const question = draft.trim();
    if (!question || !inputKey || !output || busy) return;
    setDraft("");
    setBusy(true);
    const image = attached;
    setAttached(null);
    setMessages((m) => [...m, { role: "user", text: question, ...(image ? { image } : {}) }]);
    try {
      const inputs = chatInputs(inputNodes, inputKey, question, conversationId, consent);
      if (imageKey) {
        const fixed = runInputs[imageKey];
        if (image) inputs[imageKey] = image.id;
        else if (typeof fixed === "string") inputs[imageKey] = fixed;
      }
      const result = await onAsk(inputs, output.id);
      setMessages((m) => [...m, result ? toMessage(result, output.key) : { role: "assistant", text: "", error: "The run did not start" }]);
    } finally {
      setBusy(false);
    }
  };

  if (textInputs.length === 0 || outputs.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        Chat needs a text <b>Manual Input</b> (the question) and an <b>Output</b> node. Start from the
        <i> RAG answers with citations</i> template to try it.
      </p>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <label className="flex items-center gap-1">
          Ask into
          <select className="am-input !w-auto !py-0.5 text-xs" value={inputKey} onChange={(e) => setInputKey(e.target.value)}>
            {textInputs.map((n) => (
              <option key={n.id} value={String(n.config.key)}>
                {n.ui?.label || String(n.config.key)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          answer from
          <select className="am-input !w-auto !py-0.5 text-xs" value={target} onChange={(e) => setTarget(e.target.value)}>
            {outputs.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {asksConsent && (
          <label
            className="flex items-center gap-1 text-slate-600"
            title="When on, the assistant may save facts you share to long-term memory. You can review and delete them in the Memory tab."
          >
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            Remember facts I share
          </label>
        )}
        <span className="text-slate-400">
          {remembersConversation
            ? "This chat remembers earlier messages."
            : "Each question runs only what this output needs. No memory between questions."}
        </span>
        {(messages.length > 0 || remembersConversation) && (
          <button
            type="button"
            className="ml-auto text-slate-500 hover:text-slate-800"
            onClick={() => {
              setMessages([]);
              setConversationId(newConversationId());
            }}
            title={remembersConversation ? "Start a new conversation (the assistant forgets this chat's messages)" : undefined}
          >
            {remembersConversation ? "New chat" : "Clear"}
          </button>
        )}
      </div>

      <div ref={scroller} className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
        {messages.length === 0 && <p className="pt-2 text-xs text-slate-400">Ask a question about your documents.</p>}
        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="flex justify-end">
              <div className="max-w-[80%] rounded-lg bg-accent-100 px-3 py-2 text-sm text-accent-900">
                {m.image && (
                  <div className="mb-1.5">
                    <ImageThumb id={m.image.id} name={m.image.name} size="md" />
                  </div>
                )}
                {m.text}
              </div>
            </div>
          ) : (
            <div key={i} className="max-w-[90%] rounded-lg border border-slate-200 bg-surface px-3 py-2">
              {m.error ? (
                <div className="text-sm text-red-700">{m.error}</div>
              ) : (
                <AnswerText message={m} onCite={(n) => setActiveCite(`${i}:${n}`)} />
              )}
              {m.steps && m.steps.length > 0 && <AgentSteps steps={m.steps} stopReason={m.stopReason} />}
              {m.sql && <SqlResult sql={m.sql} />}
              {m.citations && m.citations.length > 0 && (
                <ol className="mt-2 space-y-1 border-t border-slate-100 pt-2">
                  {m.citations.map((c) => (
                    <li
                      key={c.n}
                      className={`rounded px-1.5 py-1 text-xs ${activeCite === `${i}:${c.n}` ? "bg-accent-50" : ""}`}
                    >
                      <span className="mr-1.5 font-semibold text-accent-700">[{c.n}]</span>
                      <span className="font-medium text-slate-700">{sourceLabel(c)}</span>
                      {c.excerpt && <div className="mt-0.5 line-clamp-2 text-slate-500">{c.excerpt}</div>}
                    </li>
                  ))}
                </ol>
              )}
              {m.invalid && m.invalid.length > 0 && (
                <div className="mt-1 text-[11px] text-amber-700">
                  Removed citation{m.invalid.length > 1 ? "s" : ""} to non-existent source{m.invalid.length > 1 ? "s" : ""}: {m.invalid.join(", ")}
                </div>
              )}
              {m.meta && (
                <div className="mt-1.5 text-[10px] text-slate-400">
                  {m.meta.ms !== undefined && `${(m.meta.ms / 1000).toFixed(1)} s`}
                  {m.meta.tokens ? ` · ${m.meta.tokens.toLocaleString()} tokens` : ""}
                  {m.meta.cost ? ` · $${m.meta.cost.toFixed(5)}` : ""}
                </div>
              )}
            </div>
          ),
        )}
        {busy && <div className="text-xs text-slate-400">Thinking…</div>}
      </div>

      {attached && (
        <div className="mt-2 flex items-center gap-2 text-xs text-slate-600">
          <ImageThumb id={attached.id} name={attached.name} size="sm" />
          <span className="truncate">{attached.name}</span>
          <button type="button" className="text-slate-400 hover:text-red-600" onClick={() => setAttached(null)} aria-label="Remove attached image">
            ×
          </button>
        </div>
      )}
      <form
        className="mt-2 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        {imageKey && (
          <>
            <button
              type="button"
              className="am-btn !px-2.5"
              onClick={() => attachInput.current?.click()}
              disabled={!ready || busy || attaching}
              title="Attach an image (goes to the graph's Image Input)"
              aria-label="Attach an image"
            >
              {attaching ? "…" : (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <rect x="3" y="3" width="18" height="18" rx="2" />
                  <circle cx="8.5" cy="8.5" r="1.5" />
                  <path d="M21 15l-5-5L5 21" />
                </svg>
              )}
            </button>
            <input
              ref={attachInput}
              type="file"
              accept={IMAGE_ACCEPT}
              className="hidden"
              onChange={(e) => {
                const picked = e.target.files ? Array.from(e.target.files) : [];
                e.target.value = "";
                void attach(picked);
              }}
            />
          </>
        )}
        <input
          className="am-input"
          placeholder={ready ? "Ask a question…" : "Waiting for the API…"}
          value={draft}
          disabled={!ready || busy}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Question"
        />
        <button type="submit" className="am-btn-primary" disabled={!ready || busy || !draft.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}

/** NL-to-SQL answers: the exact SQL that ran and the first rows, so the number can be checked. */
function SqlResult({ sql }: { sql: NonNullable<Message["sql"]> }) {
  const [open, setOpen] = useState(true);
  const shown = sql.rows.slice(0, 10);
  return (
    <div className="mt-2 border-t border-slate-100 pt-2 text-xs">
      <button type="button" className="text-slate-500 hover:text-slate-800" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "▾" : "▸"} SQL and {sql.total} row{sql.total === 1 ? "" : "s"}
        {sql.truncated && <span className="text-amber-700"> (truncated)</span>}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-[11px] text-slate-700">{sql.query}</pre>
          {shown.length > 0 && (
            <div className="overflow-x-auto">
              <table className="min-w-full border-collapse font-mono text-[11px]">
                <thead>
                  <tr>
                    {sql.columns.map((c) => (
                      <th key={c} className="border-b border-slate-200 px-2 py-1 text-left font-semibold text-slate-600">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((row, i) => (
                    <tr key={i} className="border-b border-slate-100">
                      {sql.columns.map((c) => (
                        <td key={c} className="px-2 py-0.5 text-slate-700">
                          {row[c] === null || row[c] === undefined ? "—" : String(row[c])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {sql.rows.length > shown.length && <div className="pt-1 text-slate-400">First {shown.length} of {sql.total} rows</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** Plan-and-execute steps ({plan_step, step, result, tools}) shown like tool calls with nested tool steps. */
export function normaliseSteps(raw: unknown[]): AgentStep[] {
  return raw.map((s) => {
    const step = s as Record<string, unknown>;
    if (typeof step.plan_step === "number") {
      return {
        tool: `plan step ${step.plan_step}: ${String(step.step ?? "")}`,
        ok: step.ok !== false,
        result_summary: String(step.result ?? ""),
        substeps: Array.isArray(step.tools) ? normaliseSteps(step.tools) : undefined,
      };
    }
    return step as unknown as AgentStep;
  });
}

function AgentSteps({ steps: rawSteps, stopReason }: { steps: AgentStep[]; stopReason?: string }) {
  const steps = normaliseSteps(rawSteps);
  const [open, setOpen] = useState(false);
  const failed = steps.filter((s) => !s.ok).length;
  const tools = [...new Set(steps.map((s) => s.tool))];
  return (
    <div className="mt-2 border-t border-slate-100 pt-2 text-xs">
      <button type="button" className="text-slate-500 hover:text-slate-800" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "▾" : "▸"} Used {steps.length} tool call{steps.length === 1 ? "" : "s"}: {tools.join(", ")}
        {failed > 0 && <span className="text-amber-700"> ({failed} failed)</span>}
        {stopReason && stopReason !== "final_answer" && <span className="text-amber-700"> · stopped: {stopReason}</span>}
      </button>
      {open && (
        <ol className="mt-1.5 space-y-1.5">
          {steps.map((s, i) => (
            <li key={i} className="rounded border border-slate-100 bg-slate-50 px-2 py-1.5">
              {s.thought && <div className="mb-1 italic text-slate-500">Thought: {s.thought}</div>}
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono font-semibold text-slate-700">
                  {s.ok ? "✓" : "✗"} {s.tool}
                </span>
                {s.duration_ms !== undefined && <span className="text-[10px] text-slate-400">{s.duration_ms.toFixed(0)} ms</span>}
              </div>
              {s.arguments && (
                <div className="mt-0.5 break-all font-mono text-[11px] text-slate-500">{JSON.stringify(s.arguments)}</div>
              )}
              <div className={`mt-0.5 line-clamp-3 break-words ${s.ok ? "text-slate-600" : "text-red-700"}`}>
                {s.ok ? s.result_summary : s.error}
              </div>
              {s.substeps && s.substeps.length > 0 && (
                <div className="ml-3 border-l border-slate-200 pl-2">
                  <AgentSteps steps={s.substeps} stopReason={s.subreport?.stop_reason} />
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

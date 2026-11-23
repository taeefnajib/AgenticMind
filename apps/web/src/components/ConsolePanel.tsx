import { useMemo } from "react";

import type { Diagnostic, IRNode, JsonValue, Mode, ProjectDocument, RunOut, TraceEvent } from "../api/types";
import { ApprovalsPanel } from "./ApprovalsPanel";
import { ChatPanel, type ChatOutputOption, type ChatResult } from "./ChatPanel";
import { DiagnosticList } from "./DiagnosticList";
import { ImagePicker } from "./ImagePicker";
import { ValueView } from "./DocumentViewer";
import { MemoryPanel } from "./MemoryPanel";

export type ConsoleTab = "chat" | "problems" | "inputs" | "trace" | "output" | "approvals" | "memory" | "ir";

const MANUAL_INPUT = "agenticmind.core.manual_input";
const IMAGE_INPUT = "agenticmind.core.image_input";

interface Props {
  tab: ConsoleTab;
  onTab: (tab: ConsoleTab) => void;
  mode: Mode;
  diagnostics: Diagnostic[];
  onSelectDiagnostic: (d: Diagnostic) => void;
  inputNodes: IRNode[];
  /** Image Input nodes: their run input is an uploaded image's file ID. */
  imageInputNodes: IRNode[];
  runInputs: Record<string, JsonValue>;
  onRunInputs: (inputs: Record<string, JsonValue>) => void;
  run: RunOut | null;
  events: TraceEvent[];
  onSelectNode: (id: string) => void;
  document: () => ProjectDocument;
  chatKey: string;
  projectId?: string;
  approvalsKey?: unknown;
  approvalCount?: number;
  onApprovalCount?: (n: number) => void;
  onResume: (runId: string, body: { responses?: Record<string, JsonValue>; force?: boolean }) => Promise<void>;
  chatOutputs: ChatOutputOption[];
  chatDisabled: boolean;
  onAsk: (inputs: Record<string, JsonValue>, target: string) => Promise<ChatResult | null>;
}

export function isManualInput(n: IRNode) {
  return n.type === MANUAL_INPUT;
}

export function isImageInput(n: IRNode) {
  return n.type === IMAGE_INPUT;
}

export function ConsolePanel(props: Props) {
  const { tab, onTab, mode, diagnostics, run, events } = props;
  const errors = diagnostics.filter((d) => d.severity === "error").length;
  const tabs: { id: ConsoleTab; label: string; badge?: string | number; tone?: string }[] = [
    { id: "chat", label: "Chat" },
    { id: "problems", label: "Problems", badge: diagnostics.length || undefined, tone: errors ? "bg-red-100 text-red-700" : "bg-amber-100 text-amber-800" },
    { id: "inputs", label: "Run inputs" },
    { id: "trace", label: "Trace", badge: events.length || undefined, tone: "bg-slate-200 text-slate-700" },
    {
      id: "approvals",
      label: "Approvals",
      badge: props.approvalCount || undefined,
      tone: "bg-amber-100 text-amber-800",
    },
    { id: "memory", label: "Memory" },
    { id: "output", label: "Output", badge: run?.status, tone: run?.status === "failed" ? "bg-red-100 text-red-700" : run?.status === "succeeded" ? "bg-emerald-100 text-emerald-700" : "bg-slate-200 text-slate-700" },
  ];
  if (mode === "developer") tabs.push({ id: "ir", label: "IR (live)" });

  return (
    <section className={`flex shrink-0 flex-col border-t border-slate-200 bg-surface ${tab === "chat" ? "h-96" : "h-64"}`}>
      <nav className="flex items-center gap-1 border-b border-slate-200 px-2" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => onTab(t.id)}
            className={`flex items-center gap-1.5 border-b-2 px-3 py-2 text-xs font-medium ${
              tab === t.id ? "border-accent-600 text-accent-700" : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {t.label}
            {t.badge !== undefined && <span className={`rounded px-1.5 text-[10px] ${t.tone}`}>{t.badge}</span>}
          </button>
        ))}
      </nav>
      <div className={tab === "chat" ? "min-h-0 flex-1 p-3" : "hidden"}>
        <ChatPanel
          key={props.chatKey}
          inputNodes={props.inputNodes}
          imageInputs={props.imageInputNodes}
          runInputs={props.runInputs}
          outputs={props.chatOutputs}
          disabled={props.chatDisabled}
          onAsk={props.onAsk}
        />
      </div>
      <div className={tab === "chat" ? "hidden" : "flex-1 overflow-auto p-3"}>
        {tab === "problems" &&
          (diagnostics.length ? (
            <DiagnosticList diagnostics={diagnostics} onSelect={props.onSelectDiagnostic} />
          ) : (
            <p className="text-xs text-emerald-700">No problems. The graph is valid.</p>
          ))}
        {tab === "inputs" && <RunInputs {...props} />}
        {tab === "trace" && (
          <>
            {props.run && events.length > 0 && (
              <div className="mb-1 flex justify-end gap-3 text-[11px]">
                <a className="text-accent-700 underline" href={`/api/runs/${props.run.id}/otlp`} download={`${props.run.id}.otlp.json`}
                  title="This run as an OpenTelemetry (OTLP/JSON) trace, without prompt/output content">
                  Download OpenTelemetry trace
                </a>
              </div>
            )}
            <TraceTable events={events} onSelectNode={props.onSelectNode} />
          </>
        )}
        {tab === "output" && <RunOutput run={run} events={events} onResume={props.onResume} />}
        {tab === "approvals" && (
          <ApprovalsPanel
            projectId={props.projectId}
            refreshKey={props.approvalsKey}
            onCount={props.onApprovalCount}
            onResume={(runId, responses) => props.onResume(runId, { responses })}
          />
        )}
        {tab === "memory" && <MemoryPanel projectId={props.projectId} refreshKey={run?.status} />}
        {tab === "ir" && (
          <pre className="font-mono text-[11px] leading-relaxed text-slate-800">{JSON.stringify(props.document(), null, 2)}</pre>
        )}
      </div>
    </section>
  );
}

function RunInputs({ inputNodes, imageInputNodes, runInputs, onRunInputs }: Props) {
  if (inputNodes.length === 0 && imageInputNodes.length === 0) {
    return (
      <p className="text-xs text-slate-500">This graph has no Manual Input or Image Input nodes, so runs take no inputs.</p>
    );
  }
  const set = (key: string, value: JsonValue | undefined) => {
    const next = { ...runInputs };
    if (value === undefined) delete next[key];
    else next[key] = value;
    onRunInputs(next);
  };
  return (
    <div className="grid max-w-3xl grid-cols-1 gap-3 md:grid-cols-2">
      {inputNodes.map((n) => {
        const key = String(n.config.key ?? "input");
        const type = String(n.config.value_type ?? "String");
        const def = n.config.default;
        const value = runInputs[key];
        const id = `run-input-${key}`;
        return (
          <div key={n.id}>
            <label htmlFor={id} className="mb-1 flex justify-between text-xs font-medium text-slate-700">
              <span>
                {n.ui?.label || key} <span className="font-mono text-slate-400">({type})</span>
              </span>
              <span className="font-mono text-[10px] text-slate-400">{key}</span>
            </label>
            {type === "Boolean" ? (
              <input id={id} type="checkbox" className="h-4 w-4 accent-accent-600" checked={Boolean(value ?? def)} onChange={(e) => set(key, e.target.checked)} />
            ) : type === "Number" ? (
              <input
                id={id}
                type="number"
                className="am-input"
                placeholder={def !== undefined && def !== null ? `default: ${String(def)}` : "required"}
                value={typeof value === "number" ? value : ""}
                onChange={(e) => set(key, e.target.value === "" ? undefined : Number(e.target.value))}
              />
            ) : type === "JSON" ? (
              <JsonInput id={id} value={value} onChange={(v) => set(key, v)} />
            ) : (
              <input
                id={id}
                className="am-input"
                placeholder={typeof def === "string" ? `default: ${def}` : "required"}
                value={typeof value === "string" ? value : ""}
                onChange={(e) => set(key, e.target.value === "" ? undefined : e.target.value)}
              />
            )}
          </div>
        );
      })}
      {imageInputNodes.map((n) => {
        const key = String(n.config.key ?? "image");
        const value = runInputs[key];
        const id = `run-input-${key}`;
        return (
          <div key={n.id}>
            <label htmlFor={id} className="mb-1 flex justify-between text-xs font-medium text-slate-700">
              <span>
                {n.ui?.label || key} <span className="font-mono text-slate-400">(Image)</span>
              </span>
              <span className="font-mono text-[10px] text-slate-400">{key}</span>
            </label>
            <ImagePicker id={id} value={typeof value === "string" ? value : undefined} onChange={(v) => set(key, v)} />
          </div>
        );
      })}
      <p className="text-[11px] text-slate-500 md:col-span-2">Empty fields fall back to the node's default value.</p>
    </div>
  );
}

function JsonInput({ id, value, onChange }: { id: string; value: JsonValue | undefined; onChange: (v: JsonValue | undefined) => void }) {
  return (
    <textarea
      id={id}
      className="am-input font-mono text-xs"
      defaultValue={value === undefined ? "" : JSON.stringify(value)}
      placeholder='{"key": "value"}'
      onBlur={(e) => {
        try {
          onChange(e.target.value.trim() ? (JSON.parse(e.target.value) as JsonValue) : undefined);
        } catch {
          /* keep previous value; invalid JSON is ignored */
        }
      }}
    />
  );
}

function TraceTable({ events, onSelectNode }: { events: TraceEvent[]; onSelectNode: (id: string) => void }) {
  const t0 = events[0] ? Date.parse(events[0].timestamp) : 0;
  if (events.length === 0) return <p className="text-xs text-slate-500">Run the graph to see its execution trace.</p>;
  return (
    <table className="w-full text-left font-mono text-[11px]">
      <thead className="sticky top-0 bg-surface text-slate-500">
        <tr>
          <th className="py-1 pr-3 font-medium">#</th>
          <th className="py-1 pr-3 font-medium">+ms</th>
          <th className="py-1 pr-3 font-medium">event</th>
          <th className="py-1 pr-3 font-medium">node</th>
          <th className="py-1 pr-3 font-medium">attempt</th>
          <th className="py-1 pr-3 font-medium">duration</th>
          <th className="py-1 font-medium">details</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e) => (
          <tr
            key={e.seq}
            className={`cursor-pointer border-t border-slate-100 hover:bg-slate-50 ${e.error ? "text-red-700" : ""}`}
            onClick={() => e.node_id && onSelectNode(e.node_id)}
          >
            <td className="py-1 pr-3 text-slate-400">{e.seq}</td>
            <td className="py-1 pr-3 text-slate-400">{(Date.parse(e.timestamp) - t0).toFixed(0)}</td>
            <td className="py-1 pr-3">{e.event_type}</td>
            <td className="py-1 pr-3">{e.node_id ?? ""}</td>
            <td className="py-1 pr-3">{e.attempt ?? ""}</td>
            <td className="py-1 pr-3">{e.duration_ms !== undefined ? `${e.duration_ms.toFixed(1)}ms` : ""}</td>
            <td className="max-w-[480px] truncate py-1" title={eventDetails(e)}>
              {eventDetails(e)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function eventDetails(e: TraceEvent): string {
  if (e.error) return `${e.error.code}: ${e.error.message}`;
  if (e.usage) return `${e.usage.provider}/${e.usage.model} ${e.usage.input_tokens}→${e.usage.output_tokens} tokens`;
  if (e.event_type === "node_skipped") return String(e.data?.reason ?? "");
  if (e.outputs_summary !== undefined) return JSON.stringify(e.outputs_summary);
  if (e.event_type === "node_event") return JSON.stringify(e.data);
  return "";
}

function RunOutput({
  run,
  events,
  onResume,
}: {
  run: RunOut | null;
  events: TraceEvent[];
  onResume: (runId: string, body: { force?: boolean }) => Promise<void>;
}) {
  const totals = useMemo(() => {
    let input = 0;
    let output = 0;
    let cost = 0;
    for (const e of events) {
      if (e.usage) {
        input += e.usage.input_tokens;
        output += e.usage.output_tokens;
        cost += e.usage.estimated_cost_usd ?? 0;
      }
    }
    const end = [...events].reverse().find((e) => e.event_type.startsWith("run_") && e.event_type !== "run_started");
    return { input, output, cost, duration: end?.duration_ms };
  }, [events]);

  if (!run) return <p className="text-xs text-slate-500">No run yet.</p>;
  return (
    <div className="flex gap-6">
      <dl className="grid shrink-0 grid-cols-[90px_auto] gap-y-1 text-xs">
        <dt className="text-slate-500">Run</dt>
        <dd className="font-mono">{run.id}</dd>
        <dt className="text-slate-500">Status</dt>
        <dd className="font-medium">
          {run.status}
          {run.error?.code === "RUN_INTERRUPTED" && (
            <button
              type="button"
              className="am-btn ml-2 !px-2 !py-0.5 text-[11px]"
              title="Continue from the last checkpoint: finished steps are not run again"
              onClick={() => void onResume(run.id, {})}
            >
              Resume
            </button>
          )}
        </dd>
        <dt className="text-slate-500">Revision</dt>
        <dd>{run.revision}</dd>
        <dt className="text-slate-500">Duration</dt>
        <dd>{totals.duration !== undefined ? `${totals.duration.toFixed(1)} ms` : "…"}</dd>
        <dt className="text-slate-500">Tokens</dt>
        <dd>
          {totals.input} in / {totals.output} out
        </dd>
        <dt className="text-slate-500">Est. cost</dt>
        <dd>${totals.cost.toFixed(4)}</dd>
      </dl>
      <div className="min-w-0 flex-1">
        {run.error && (
          <div className="mb-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-800">
            <span className="font-mono font-semibold">{run.error.code}</span>
            {run.error.node_id && <span className="font-mono"> @ {run.error.node_id}</span>}: {run.error.message}
          </div>
        )}
        {run.outputs &&
          Object.entries(run.outputs).map(([key, value]) => (
            <div key={key} className="mb-3">
              <div className="mb-1 font-mono text-[10px] font-semibold uppercase text-slate-500">{key}</div>
              <ValueView value={value} />
            </div>
          ))}
        {!run.outputs && !run.error && <p className="text-xs text-slate-500">Running…</p>}
      </div>
    </div>
  );
}

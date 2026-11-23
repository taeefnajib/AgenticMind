import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { Diagnostic, IRNode, JsonValue, NodeManifest, RunOut, TraceEvent } from "../api/types";
import { effectiveBranches, effectivePortType, manifestKey, type AmFlowEdge, type AmFlowNode } from "../lib/graph";
import { categoryStyle, chipStyle, portColor, STATUS_STYLE } from "../lib/theme";
import { useWorkbench } from "../lib/workbench";
import { ConfigForm } from "./ConfigForm";
import { DiagnosticList } from "./DiagnosticList";
import { ValueView } from "./DocumentViewer";
import { FilesPanel } from "./Files";
import { GitPanel } from "./GitPanel";
import { TriggerPanel } from "./TriggerPanel";
import { isTriggerType } from "../lib/triggers";
import { mcpEndpoint, mcpToolName, type McpExposure } from "../lib/mcp";

export interface ProjectMeta {
  name: string;
  description: string;
  mcp: McpExposure;
}

interface Props {
  node: AmFlowNode | null;
  edge: AmFlowEdge | null;
  meta: ProjectMeta;
  onMetaChange: (meta: ProjectMeta) => void;
  dirty?: boolean;
  onReloadProject?: () => void;
  onNodeChange: (id: string, update: (ir: IRNode) => IRNode) => void;
  onDeleteNode: (id: string) => void;
  onDeleteEdge: (id: string) => void;
  diagnostics: Diagnostic[];
  run: RunOut | null;
  events: TraceEvent[];
  onSelectDiagnostic: (d: Diagnostic) => void;
}

export function Inspector(props: Props) {
  const { node, edge } = props;
  return (
    <aside className="flex h-full w-80 shrink-0 flex-col overflow-y-auto border-l border-slate-200 bg-surface">
      {node ? <NodeInspector {...props} node={node} /> : edge ? <EdgeInspector {...props} edge={edge} /> : <ProjectInspector {...props} />}
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-b border-slate-100 p-4">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      {children}
    </section>
  );
}

function ProjectInspector({ meta, onMetaChange, diagnostics, onSelectDiagnostic, dirty, onReloadProject }: Props) {
  const { files } = useWorkbench();
  const errors = diagnostics.filter((d) => d.severity === "error").length;
  return (
    <>
      <Section title="Project">
        <label className="mb-1 block text-xs font-medium text-slate-700" htmlFor="proj-name">
          Name
        </label>
        <input id="proj-name" className="am-input mb-3" value={meta.name} onChange={(e) => onMetaChange({ ...meta, name: e.target.value })} />
        <label className="mb-1 block text-xs font-medium text-slate-700" htmlFor="proj-desc">
          Description
        </label>
        <textarea
          id="proj-desc"
          className="am-input min-h-[70px]"
          value={meta.description}
          onChange={(e) => onMetaChange({ ...meta, description: e.target.value })}
        />
      </Section>
      <Section title="GitHub">
        <GitPanel projectId={files.projectId} dirty={Boolean(dirty)} onReload={() => onReloadProject?.()} />
      </Section>
      <Section title="MCP tool">
        <McpExposeControl meta={meta} onMetaChange={onMetaChange} />
      </Section>
      <Section title={`Validation${errors ? ` · ${errors} error${errors > 1 ? "s" : ""}` : ""}`}>
        {diagnostics.length === 0 ? (
          <p className="text-xs text-emerald-700">No problems found.</p>
        ) : (
          <DiagnosticList diagnostics={diagnostics} onSelect={onSelectDiagnostic} compact />
        )}
      </Section>
      <Section title="Files">
        <FilesPanel />
      </Section>
      <Section title="Tips">
        <ul className="list-disc space-y-1 pl-4 text-xs text-slate-600">
          <li>Select a node to configure it.</li>
          <li>Coloured ports carry typed data; amber squares carry control flow.</li>
          <li>Ctrl+S saves a new revision; Run executes the saved revision.</li>
        </ul>
      </Section>
    </>
  );
}

export function McpExposeControl({ meta, onMetaChange }: Pick<Props, "meta" | "onMetaChange">) {
  const mcp = meta.mcp;
  const name = mcpToolName(mcp.tool_name || meta.name);
  const endpoint = mcpEndpoint(window.location.origin);
  const set = (next: McpExposure) => onMetaChange({ ...meta, mcp: next });
  return (
    <div className="space-y-2 text-xs">
      <label className="flex items-center gap-2 text-slate-700">
        <input type="checkbox" checked={mcp.expose} onChange={(e) => set({ ...mcp, expose: e.target.checked })} />
        Expose as MCP tool
      </label>
      <p className="text-slate-500">
        MCP clients (Claude, Cursor, other agents) can call this workflow. Its Manual Inputs become the tool&apos;s
        arguments. Save to apply.
      </p>
      {mcp.expose && (
        <>
          <label className="block font-medium text-slate-700" htmlFor="mcp-tool-name">
            Tool name
          </label>
          <input
            id="mcp-tool-name"
            className="am-input"
            placeholder={mcpToolName(meta.name)}
            value={mcp.tool_name ?? ""}
            onChange={(e) => set({ ...mcp, tool_name: e.target.value })}
          />
          <p className="text-slate-500">
            Called as <code className="font-mono text-slate-700">{name}</code> at
          </p>
          <code className="block break-all rounded bg-slate-100 px-2 py-1 font-mono text-[11px] text-slate-700">{endpoint}</code>
          <p className="text-slate-500">
            Streamable HTTP. When <code className="font-mono">AGENTICMIND_MCP_TOKEN</code> is set, clients send it as a
            bearer token.
          </p>
        </>
      )}
    </div>
  );
}

function NodeInspector({ node, onNodeChange, onDeleteNode, diagnostics, run, events, onSelectDiagnostic }: Props & { node: AmFlowNode }) {
  const { manifests, mode, nodeStatus, files } = useWorkbench();
  const ir = node.data.ir;
  const manifest = manifests.get(manifestKey(ir.type, ir.version));
  const nodeDiags = diagnostics.filter((d) => d.node_id === ir.id);
  const errorsByField = new Map<string, string[]>();
  for (const d of nodeDiags) {
    if (d.path?.startsWith("config.")) {
      const field = d.path.slice(7).split(".")[0] ?? "";
      errorsByField.set(field, [...(errorsByField.get(field) ?? []), d.message]);
    }
  }
  const status = nodeStatus[ir.id];
  const lastEvent = [...events].reverse().find((e) => e.node_id === ir.id && (e.event_type === "node_succeeded" || e.event_type === "node_failed"));

  return (
    <>
      <Section title="Node">
        <div className="mb-2 flex items-center gap-2">
          <span className="rounded px-1.5 py-0.5 text-[10px] font-medium" style={chipStyle(categoryStyle(manifest?.category ?? "").color)}>
            {manifest?.category ?? "unknown"}
          </span>
          {manifest && <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">trust: {manifest.trust}</span>}
        </div>
        <div className="text-base font-semibold text-slate-900">{manifest?.display_name ?? ir.type}</div>
        <p className="mt-1 text-xs text-slate-600">{manifest?.description}</p>
        <label className="mb-1 mt-3 block text-xs font-medium text-slate-700" htmlFor="node-label">
          Label
        </label>
        <input
          id="node-label"
          className="am-input"
          placeholder={manifest?.display_name}
          value={ir.ui?.label ?? ""}
          onChange={(e) => onNodeChange(ir.id, (n) => ({ ...n, ui: { ...n.ui, label: e.target.value || null } }))}
        />
        {mode === "developer" && (
          <dl className="mt-3 grid grid-cols-[70px_1fr] gap-y-1 font-mono text-[11px]">
            <dt className="text-slate-500">id</dt>
            <dd className="truncate">{ir.id}</dd>
            <dt className="text-slate-500">type</dt>
            <dd className="truncate">{ir.type}</dd>
            <dt className="text-slate-500">version</dt>
            <dd>{ir.version}</dd>
          </dl>
        )}
      </Section>

      {isTriggerType(ir.type) && (
        <Section title="Trigger">
          <TriggerPanel projectId={files.projectId} nodeId={ir.id} />
        </Section>
      )}

      {nodeDiags.length > 0 && (
        <Section title="Problems">
          <DiagnosticList diagnostics={nodeDiags} onSelect={onSelectDiagnostic} compact />
        </Section>
      )}

      {manifest && (
        <Section title="Configuration">
          <ConfigForm
            manifest={manifest}
            config={ir.config}
            mode={mode}
            errorsByField={errorsByField}
            onChange={(config) => onNodeChange(ir.id, (n) => ({ ...n, config }))}
          />
        </Section>
      )}

      {manifest && <PortsSection manifest={manifest} config={ir.config} />}

      {run && <RunSection runId={run.id} nodeId={ir.id} status={status} lastEvent={lastEvent} />}

      {manifest && mode === "developer" && (
        <Section title="Execution policy">
          <div className="grid grid-cols-2 gap-2">
            <label className="text-xs text-slate-700">
              Timeout (s)
              <input
                type="number"
                className="am-input mt-1"
                placeholder={String(manifest.execution.timeout_seconds)}
                value={ir.execution?.timeout_seconds ?? ""}
                onChange={(e) =>
                  onNodeChange(ir.id, (n) => ({
                    ...n,
                    execution: { ...n.execution, timeout_seconds: e.target.value ? Number(e.target.value) : null },
                  }))
                }
              />
            </label>
            <label className="text-xs text-slate-700">
              Max attempts
              <input
                type="number"
                className="am-input mt-1"
                min={1}
                max={10}
                placeholder={String(manifest.execution.max_attempts)}
                value={ir.execution?.max_attempts ?? ""}
                onChange={(e) =>
                  onNodeChange(ir.id, (n) => ({
                    ...n,
                    execution: { ...n.execution, max_attempts: e.target.value ? Number(e.target.value) : null },
                  }))
                }
              />
            </label>
          </div>
          <p className="mt-2 text-[11px] text-slate-500">
            Permissions: {Object.entries(manifest.permissions).filter(([, v]) => v).map(([k]) => k).join(", ") || "none"}
            {" · "}
            {manifest.execution.idempotent ? "idempotent" : "not idempotent (timeouts never retried)"}
          </p>
        </Section>
      )}

      {mode === "developer" && (
        <Section title="IR node">
          <pre className="max-h-64 overflow-auto rounded bg-slate-900 p-2 font-mono text-[10px] leading-relaxed text-slate-100">
            {JSON.stringify(ir, null, 2)}
          </pre>
        </Section>
      )}

      <div className="p-4">
        <button type="button" className="am-btn w-full justify-center text-red-600" onClick={() => onDeleteNode(ir.id)}>
          Delete node
        </button>
      </div>
    </>
  );
}

function PortsSection({ manifest, config }: { manifest: NodeManifest; config: Record<string, JsonValue> }) {
  const { dark } = useWorkbench();
  if (!manifest.inputs.length && !manifest.outputs.length) return null;
  return (
    <Section title="Ports">
      <table className="w-full text-xs">
        <tbody>
          {manifest.inputs.map((p) => (
            <tr key={`i-${p.name}`} title={p.description}>
              <td className="py-0.5 text-slate-400">in</td>
              <td className="py-0.5 font-medium">
                {p.name}
                {p.required && <span className="text-red-500">*</span>}
              </td>
              <td className="py-0.5 text-right font-mono" style={{ color: portColor(effectivePortType(p, config), dark) }}>
                {effectivePortType(p, config)}
                {p.cardinality === "multiple" ? "[]" : ""}
              </td>
            </tr>
          ))}
          {manifest.outputs.map((p) => (
            <tr key={`o-${p.name}`} title={p.description}>
              <td className="py-0.5 text-slate-400">out</td>
              <td className="py-0.5 font-medium">{p.name}</td>
              <td className="py-0.5 text-right font-mono" style={{ color: portColor(effectivePortType(p, config), dark) }}>
                {effectivePortType(p, config)}
              </td>
            </tr>
          ))}
          {!(effectiveBranches(manifest, config).length === 1 && effectiveBranches(manifest, config)[0] === "done") && (
            <tr>
              <td className="py-0.5 text-slate-400">ctrl</td>
              <td className="py-0.5 font-medium" colSpan={2}>
                {effectiveBranches(manifest, config).join(" / ")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </Section>
  );
}

function RunSection({
  runId,
  nodeId,
  status,
  lastEvent,
}: {
  runId: string;
  nodeId: string;
  status: string | undefined;
  lastEvent: TraceEvent | undefined;
}) {
  const [outputs, setOutputs] = useState<Record<string, JsonValue> | null>(null);
  useEffect(() => {
    setOutputs(null);
    if (status !== "succeeded") return;
    let cancelled = false;
    api
      .nodeOutputs(runId, nodeId)
      .then((r) => !cancelled && setOutputs(r.outputs))
      .catch(() => !cancelled && setOutputs(null));
    return () => {
      cancelled = true;
    };
  }, [runId, nodeId, status]);

  const st = status as keyof typeof STATUS_STYLE | undefined;
  return (
    <Section title="Last run">
      <div className="mb-2 flex items-center gap-2 text-xs">
        <span className={`rounded px-1.5 py-0.5 font-medium ${st ? STATUS_STYLE[st].badge : "bg-slate-100 text-slate-500"}`}>
          {status ?? "not run"}
        </span>
        {lastEvent?.duration_ms !== undefined && <span className="text-slate-500">{lastEvent.duration_ms.toFixed(1)} ms</span>}
        {lastEvent?.usage && (
          <span className="text-slate-500">
            {lastEvent.usage.input_tokens}→{lastEvent.usage.output_tokens} tok
          </span>
        )}
      </div>
      {lastEvent?.error && (
        <div className="mb-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-800">
          <div className="font-mono font-semibold">{lastEvent.error.code}</div>
          <div>{lastEvent.error.message}</div>
        </div>
      )}
      {outputs &&
        Object.entries(outputs).map(([port, value]) => (
          <div key={port} className="mb-2">
            <div className="mb-1 font-mono text-[10px] font-semibold uppercase text-slate-500">{port}</div>
            <ValueView value={value} />
          </div>
        ))}
    </Section>
  );
}

function EdgeInspector({ edge, onDeleteEdge }: Props & { edge: AmFlowEdge }) {
  const kind = edge.data?.kind ?? "data";
  return (
    <>
      <Section title={kind === "control" ? "Control edge" : "Data edge"}>
        <dl className="grid grid-cols-[60px_1fr] gap-y-1 text-xs">
          <dt className="text-slate-500">from</dt>
          <dd className="font-mono">
            {edge.source}.{edge.sourceHandle?.replace(/^(out:|ctrl-out:)/, "")}
          </dd>
          <dt className="text-slate-500">to</dt>
          <dd className="font-mono">
            {edge.target}.{edge.targetHandle?.replace(/^(in:|ctrl-in)/, "") || "trigger"}
          </dd>
          <dt className="text-slate-500">id</dt>
          <dd className="font-mono">{edge.id}</dd>
        </dl>
        <p className="mt-3 text-xs text-slate-600">
          {kind === "control"
            ? "The target runs only if this branch is activated. Control edges carry no data."
            : "Carries a typed value from an output port to an input port."}
        </p>
      </Section>
      <div className="p-4">
        <button type="button" className="am-btn w-full justify-center text-red-600" onClick={() => onDeleteEdge(edge.id)}>
          Delete edge
        </button>
      </div>
    </>
  );
}

import { Handle, Position, type NodeProps } from "@xyflow/react";
import { memo } from "react";

import { CTRL_IN, ctrlOut, dataIn, dataOut, effectivePortType, manifestKey, type AmFlowNode } from "../lib/graph";
import { categoryStyle, portColor, STATUS_STYLE } from "../lib/theme";
import { useWorkbench } from "../lib/workbench";

function AmNodeView({ id, data, selected }: NodeProps<AmFlowNode>) {
  const { manifests, nodeStatus, diagnosticsByNode, mode } = useWorkbench();
  const ir = data.ir;
  const manifest = manifests.get(manifestKey(ir.type, ir.version));
  const status = nodeStatus[id];
  const diags = diagnosticsByNode.get(id) ?? [];
  const errors = diags.filter((d) => d.severity === "error").length;
  const warnings = diags.filter((d) => d.severity === "warning").length;
  const cat = categoryStyle(manifest?.category ?? "unknown");
  const branches = manifest?.branches ?? ["done"];
  const showBranchLabels = !(branches.length === 1 && branches[0] === "done");

  return (
    <div
      className={[
        "relative w-60 rounded-lg border bg-white text-left shadow-sm transition-shadow",
        selected ? "border-sky-500 shadow-md" : "border-slate-200",
        errors ? "border-red-400" : "",
        status ? STATUS_STYLE[status].ring : "",
      ].join(" ")}
    >
      {/* control-flow trigger */}
      <Handle type="target" position={Position.Top} id={CTRL_IN} className="am-ctrl" title="Control trigger" />

      <div className={`h-1.5 rounded-t-lg ${cat.bar}`} />
      <div className="flex items-start justify-between gap-2 px-3 pb-1.5 pt-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-slate-900">
            {ir.ui?.label || manifest?.display_name || ir.type}
          </div>
          <div className="truncate font-mono text-[10px] text-slate-500">
            {mode === "developer" ? `${ir.id} · v${ir.version}` : (manifest?.category ?? ir.type)}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          {status && status !== "pending" && (
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${STATUS_STYLE[status].badge}`}>
              {STATUS_STYLE[status].text}
            </span>
          )}
          {errors > 0 && (
            <span className="rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
              {errors} error{errors > 1 ? "s" : ""}
            </span>
          )}
          {!errors && warnings > 0 && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800">
              {warnings} warning{warnings > 1 ? "s" : ""}
            </span>
          )}
        </div>
      </div>

      {!manifest && (
        <div className="px-3 pb-2 text-xs text-red-600">Node type not installed: {ir.type}@{ir.version}</div>
      )}

      {manifest && (manifest.inputs.length > 0 || manifest.outputs.length > 0) && (
        <div className="border-t border-slate-100 py-1.5">
          {manifest.inputs.map((p) => {
            const t = effectivePortType(p, ir.config);
            return (
              <div key={`i-${p.name}`} className="relative flex items-center justify-between px-3 py-0.5">
                <Handle
                  type="target"
                  position={Position.Left}
                  id={dataIn(p.name)}
                  style={{ background: portColor(t) }}
                  title={`${p.name}: ${t}${p.required ? " (required)" : ""}`}
                />
                <span className="text-xs text-slate-700">
                  {p.name}
                  {p.required && <span className="text-red-500">*</span>}
                  {p.cardinality === "multiple" && <span className="text-slate-400"> [ ]</span>}
                </span>
                <span className="font-mono text-[10px]" style={{ color: portColor(t) }}>
                  {t}
                </span>
              </div>
            );
          })}
          {manifest.outputs.map((p) => {
            const t = effectivePortType(p, ir.config);
            return (
              <div key={`o-${p.name}`} className="relative flex items-center justify-between px-3 py-0.5">
                <span className="font-mono text-[10px]" style={{ color: portColor(t) }}>
                  {t}
                </span>
                <span className="text-xs text-slate-700">{p.name}</span>
                <Handle
                  type="source"
                  position={Position.Right}
                  id={dataOut(p.name)}
                  style={{ background: portColor(t) }}
                  title={`${p.name}: ${t}`}
                />
              </div>
            );
          })}
        </div>
      )}

      {showBranchLabels && (
        <div className="flex justify-around border-t border-slate-100 px-2 pb-2 pt-1 text-[10px] font-semibold text-amber-700">
          {branches.map((b) => (
            <span key={b}>{b}</span>
          ))}
        </div>
      )}
      {!showBranchLabels && <div className="h-1.5" />}

      {branches.map((b, i) => (
        <Handle
          key={b}
          type="source"
          position={Position.Bottom}
          id={ctrlOut(b)}
          className="am-ctrl"
          style={{ left: `${((i + 1) / (branches.length + 1)) * 100}%` }}
          title={`Control branch: ${b}`}
        />
      ))}
    </div>
  );
}

export const AmNode = memo(AmNodeView);

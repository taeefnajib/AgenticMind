import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import type {
  ComparisonOut,
  DatasetOut,
  EvalCase,
  EvalCaseResult,
  EvaluationOut,
  EvaluatorInfo,
  IRNode,
  JsonValue,
} from "../api/types";
import { cleanCase, formatDelta, formatMetric, newCase, parseSource, sourceLabel, splitLines } from "../lib/evaluation";

type Tab = "datasets" | "run" | "results";
const AREAS: EvaluatorInfo["area"][] = ["retrieval", "generation", "safety", "agent", "operations"];
const DEFAULT_EVALUATORS = ["recall_at_k", "mrr", "contains_required", "citation_validity", "success", "latency_ms", "cost_usd"];

interface Props {
  open: boolean;
  projectId: string | null;
  projectName: string;
  inputNodes: IRNode[];
  outputNodes: IRNode[];
  ensureSaved: () => Promise<boolean>;
  onClose: () => void;
  notify: (text: string, tone?: "info" | "error" | "success") => void;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const d = err.detail as { message?: string; problems?: { path: string; message: string }[] } | string | undefined;
    if (typeof d === "string") return d;
    if (d?.problems?.length) return `${d.message ?? "Invalid"}: ${d.problems.map((p) => `${p.path} ${p.message}`).join("; ")}`;
    if (d?.message) return d.message;
  }
  return (err as Error).message;
}

// ---- Datasets ---------------------------------------------------------------------------------------
function CaseEditor({
  value,
  inputKeys,
  onChange,
  onDelete,
}: {
  value: EvalCase;
  inputKeys: string[];
  onChange: (c: EvalCase) => void;
  onDelete: () => void;
}) {
  const keys = [...new Set([...inputKeys, ...Object.keys(value.inputs)])];
  const field = "am-input text-xs";
  const list = (items?: string[]) => (items ?? []).join("\n");
  return (
    <div className="rounded-lg border border-slate-200 p-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <input
          className="am-input !w-40 font-mono text-xs"
          value={value.id}
          aria-label="Case id"
          onChange={(e) => onChange({ ...value, id: e.target.value })}
        />
        <button type="button" className="ml-auto text-xs text-slate-400 hover:text-red-700" onClick={onDelete}>
          Remove
        </button>
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        {keys.map((k) => (
          <label key={k} className="text-[11px] text-slate-500">
            Input <span className="font-mono">{k}</span>
            <textarea
              className={`${field} mt-0.5 min-h-12`}
              value={typeof value.inputs[k] === "string" ? (value.inputs[k] as string) : JSON.stringify(value.inputs[k] ?? "")}
              onChange={(e) => onChange({ ...value, inputs: { ...value.inputs, [k]: e.target.value as JsonValue } })}
            />
          </label>
        ))}
        <label className="text-[11px] text-slate-500">
          Reference answer (for correctness metrics)
          <textarea
            className={`${field} mt-0.5 min-h-12`}
            value={value.reference ?? ""}
            onChange={(e) => onChange({ ...value, reference: e.target.value })}
          />
        </label>
        <label className="text-[11px] text-slate-500">
          Expected sources (one per line: <span className="font-mono">file.md#Section</span>)
          <textarea
            className={`${field} mt-0.5 min-h-12 font-mono`}
            value={(value.expected_sources ?? []).map(sourceLabel).join("\n")}
            onChange={(e) =>
              onChange({
                ...value,
                expected_sources: splitLines(e.target.value)
                  .map(parseSource)
                  .filter((s): s is NonNullable<typeof s> => s !== null),
              })
            }
          />
        </label>
        <label className="text-[11px] text-slate-500">
          Must include (one phrase per line)
          <textarea className={`${field} mt-0.5`} value={list(value.must_include)} onChange={(e) => onChange({ ...value, must_include: splitLines(e.target.value) })} />
        </label>
        <label className="text-[11px] text-slate-500">
          Must not include
          <textarea className={`${field} mt-0.5`} value={list(value.must_not_include)} onChange={(e) => onChange({ ...value, must_not_include: splitLines(e.target.value) })} />
        </label>
        <label className="text-[11px] text-slate-500">
          Expected tools (agents)
          <textarea className={`${field} mt-0.5 font-mono`} value={list(value.expected_tools)} onChange={(e) => onChange({ ...value, expected_tools: splitLines(e.target.value) })} />
        </label>
      </div>
    </div>
  );
}

function DatasetsTab({
  projectId,
  inputKeys,
  datasets,
  reload,
  notify,
}: {
  projectId: string;
  inputKeys: string[];
  datasets: DatasetOut[];
  reload: () => Promise<void>;
  notify: Props["notify"];
}) {
  const [current, setCurrent] = useState<DatasetOut | null>(null);
  const [cases, setCases] = useState<EvalCase[]>([]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const open = async (id: string) => {
    const d = await api.dataset(id);
    setCurrent(d);
    setName(d.name);
    setDescription(d.description);
    setCases(d.cases ?? []);
    setError(null);
  };

  const startNew = () => {
    setCurrent(null);
    setName("New dataset");
    setDescription("");
    setCases([newCase([], inputKeys)]);
    setError(null);
  };

  const save = async () => {
    setError(null);
    const body = { name: name.trim() || "Dataset", description, cases: cases.map(cleanCase) };
    try {
      const saved = current ? await api.saveDataset(current.id, body) : await api.createDataset(projectId, body);
      setCurrent(saved);
      setCases(saved.cases ?? body.cases);
      notify(saved.created_version === false ? "No changes to save" : `Saved ${saved.name} v${saved.version}`, "success");
      await reload();
    } catch (err) {
      setError(errorText(err));
    }
  };

  const importJson = async (f: File) => {
    try {
      const data = JSON.parse(await f.text()) as { name?: string; description?: string; cases?: EvalCase[] } | EvalCase[];
      const list = Array.isArray(data) ? data : (data.cases ?? []);
      setCurrent(null);
      setName((!Array.isArray(data) && data.name) || f.name.replace(/\.json$/i, ""));
      setDescription((!Array.isArray(data) && data.description) || "");
      setCases(list);
      notify(`Loaded ${list.length} cases from ${f.name}; review and save`, "info");
    } catch {
      setError(`${f.name} is not valid JSON`);
    }
  };

  const exportJson = () => {
    const blob = new Blob([JSON.stringify({ name, description, cases: cases.map(cleanCase) }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name.trim().replace(/\s+/g, "-").toLowerCase() || "dataset"}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const editing = current !== null || cases.length > 0;
  return (
    <div className="flex h-full min-h-0 gap-4">
      <aside className="w-56 shrink-0 space-y-1 overflow-y-auto">
        <div className="flex gap-1.5 pb-1">
          <button type="button" className="am-btn-primary flex-1 !py-1 text-xs" onClick={startNew}>
            New dataset
          </button>
          <button type="button" className="am-btn !py-1 text-xs" onClick={() => file.current?.click()} title="Import cases from a JSON file">
            Import
          </button>
          <input
            ref={file}
            type="file"
            accept=".json,application/json"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void importJson(f);
              e.target.value = "";
            }}
          />
        </div>
        {datasets.length === 0 && <p className="text-xs text-slate-400">No datasets yet.</p>}
        {datasets.map((d) => (
          <button
            key={d.id}
            type="button"
            onClick={() => void open(d.id)}
            className={`block w-full rounded-md border px-2 py-1.5 text-left text-xs ${current?.id === d.id ? "border-accent-300 bg-accent-50" : "border-transparent hover:bg-slate-50"}`}
          >
            <span className="block font-medium text-slate-800">{d.name}</span>
            <span className="text-slate-500">
              v{d.version} · {d.cases_count} cases
            </span>
          </button>
        ))}
      </aside>
      <section className="min-w-0 flex-1 overflow-y-auto pr-1">
        {!editing ? (
          <div className="max-w-lg space-y-2 pt-2 text-sm text-slate-600">
            <p>
              A dataset is a versioned list of test cases: the inputs to send, and what a good result looks like (a reference
              answer, the sources that should be retrieved, phrases that must or must not appear).
            </p>
            <p className="text-xs text-slate-500">
              Saving creates a new version; evaluations record exactly which version they used, so results stay reproducible.
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            <div className="flex flex-wrap items-end gap-2">
              <label className="min-w-48 flex-1 text-[11px] text-slate-500">
                Name
                <input className="am-input mt-0.5 text-sm" value={name} onChange={(e) => setName(e.target.value)} />
              </label>
              <label className="min-w-48 flex-[2] text-[11px] text-slate-500">
                Description
                <input className="am-input mt-0.5 text-sm" value={description} onChange={(e) => setDescription(e.target.value)} />
              </label>
              {current && <span className="pb-2 text-xs text-slate-400">v{current.version}</span>}
            </div>
            {error && <p className="rounded border border-red-200 bg-red-50 p-2 text-xs text-red-800">{error}</p>}
            {cases.map((c, i) => (
              <CaseEditor
                key={i}
                value={c}
                inputKeys={inputKeys}
                onChange={(next) => setCases(cases.map((x, j) => (j === i ? next : x)))}
                onDelete={() => setCases(cases.filter((_, j) => j !== i))}
              />
            ))}
            <div className="sticky bottom-0 flex gap-2 bg-surface py-2">
              <button type="button" className="am-btn text-xs" onClick={() => setCases([...cases, newCase(cases, inputKeys)])}>
                Add case
              </button>
              <button type="button" className="am-btn text-xs" onClick={exportJson}>
                Export JSON
              </button>
              {current && (
                <button
                  type="button"
                  className="am-btn text-xs text-red-700"
                  onClick={async () => {
                    await api.deleteDataset(current.id);
                    setCurrent(null);
                    setCases([]);
                    await reload();
                  }}
                >
                  Delete
                </button>
              )}
              <button type="button" className="am-btn-primary ml-auto text-xs" onClick={() => void save()} disabled={cases.length === 0}>
                {current ? "Save new version" : "Create dataset"}
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

// ---- Run --------------------------------------------------------------------------------------------
function RunTab({
  projectId,
  datasets,
  evaluators,
  outputNodes,
  ensureSaved,
  onStarted,
}: {
  projectId: string;
  datasets: DatasetOut[];
  evaluators: EvaluatorInfo[];
  outputNodes: IRNode[];
  ensureSaved: () => Promise<boolean>;
  onStarted: (ev: EvaluationOut) => void;
}) {
  const [datasetId, setDatasetId] = useState(datasets[0]?.id ?? "");
  const [versions, setVersions] = useState<DatasetOut[]>([]);
  const [version, setVersion] = useState<number | "">("");
  const [revisions, setRevisions] = useState<{ revision: number; created_at: string | null }[]>([]);
  const [revision, setRevision] = useState<number | "">("");
  const [label, setLabel] = useState("");
  const outputs = outputNodes.map((n) => String(n.config.key ?? n.id));
  const [answer, setAnswer] = useState(outputs.includes("answer") ? "answer" : (outputs[0] ?? ""));
  const [k, setK] = useState(5);
  const [selected, setSelected] = useState<string[]>(DEFAULT_EVALUATORS);
  const [judgeModel, setJudgeModel] = useState("google/gemini-3.8-flash");
  const [judgeSecret, setJudgeSecret] = useState("OPENROUTER_API_KEY");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!datasetId && datasets[0]) setDatasetId(datasets[0].id);
  }, [datasets, datasetId]);
  useEffect(() => {
    if (!datasetId) return;
    void api.datasetVersions(datasetId).then((v) => {
      setVersions(v);
      setVersion("");
    });
  }, [datasetId]);
  useEffect(() => {
    void api.revisions(projectId).then((r) => setRevisions([...r].reverse()));
  }, [projectId]);

  const usesJudge = evaluators.some((e) => e.uses_judge && selected.includes(e.name));
  const toggle = (name: string) => setSelected((s) => (s.includes(name) ? s.filter((x) => x !== name) : [...s, name]));

  const start = async () => {
    setError(null);
    setBusy(true);
    try {
      if (!(await ensureSaved())) throw new Error("Save the project first");
      const ev = await api.startEvaluation(projectId, {
        dataset_id: datasetId,
        dataset_version: version === "" ? undefined : version,
        revision: revision === "" ? undefined : revision,
        label: label.trim(),
        config: {
          evaluators: selected,
          answer_output: answer || null,
          k,
          judge: usesJudge ? { provider: "openrouter", model: judgeModel, api_key: { $secret: judgeSecret } } : null,
        },
      });
      onStarted(ev);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (datasets.length === 0) return <p className="text-sm text-slate-500">Create a dataset first (Datasets tab).</p>;
  return (
    <div className="h-full overflow-y-auto pr-1 text-xs">
      <div className="grid max-w-4xl gap-3 md:grid-cols-3">
        <label className="text-[11px] text-slate-500">
          Dataset
          <select className="am-input mt-0.5" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
            {datasets.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-slate-500">
          Dataset version
          <select className="am-input mt-0.5" value={version} onChange={(e) => setVersion(e.target.value ? Number(e.target.value) : "")}>
            <option value="">Latest</option>
            {versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version} ({v.cases_count} cases)
              </option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-slate-500">
          Project revision (graph version)
          <select className="am-input mt-0.5" value={revision} onChange={(e) => setRevision(e.target.value ? Number(e.target.value) : "")}>
            <option value="">Latest (saves first)</option>
            {revisions.map((r) => (
              <option key={r.revision} value={r.revision}>
                rev {r.revision}
                {r.created_at ? ` · ${new Date(r.created_at).toLocaleString()}` : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-slate-500">
          Experiment label
          <input className="am-input mt-0.5" placeholder="e.g. hybrid + rerank" value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="text-[11px] text-slate-500">
          Answer comes from output
          <select className="am-input mt-0.5" value={answer} onChange={(e) => setAnswer(e.target.value)}>
            {outputs.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </label>
        <label className="text-[11px] text-slate-500">
          K (retrieval metrics)
          <input className="am-input mt-0.5" type="number" min={1} max={100} value={k} onChange={(e) => setK(Number(e.target.value) || 5)} />
        </label>
      </div>

      <h3 className="mb-1 mt-4 font-semibold text-slate-700">Evaluators</h3>
      <div className="grid max-w-5xl gap-3 md:grid-cols-3">
        {AREAS.map((area) => (
          <fieldset key={area} className="rounded border border-slate-200 p-2">
            <legend className="px-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">{area}</legend>
            {evaluators
              .filter((e) => e.area === area)
              .map((e) => (
                <label key={e.name} className="flex items-start gap-1.5 py-0.5" title={`${e.description} Aggregation: ${e.aggregation}.`}>
                  <input type="checkbox" checked={selected.includes(e.name)} onChange={() => toggle(e.name)} className="mt-0.5" />
                  <span>
                    {e.title}
                    {e.uses_judge && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-800">estimate</span>}
                  </span>
                </label>
              ))}
          </fieldset>
        ))}
      </div>
      {usesJudge && (
        <div className="mt-3 flex max-w-3xl flex-wrap items-end gap-3 rounded border border-amber-200 bg-amber-50 p-2">
          <p className="w-full text-amber-900">
            LLM-judge scores are estimates, not ground truth. The judge model, prompt version and rubric are stored with every score.
          </p>
          <label className="text-[11px] text-slate-600">
            Judge model (OpenRouter)
            <input className="am-input mt-0.5 !w-64" value={judgeModel} onChange={(e) => setJudgeModel(e.target.value)} />
          </label>
          <label className="text-[11px] text-slate-600">
            API key secret name
            <input className="am-input mt-0.5 !w-56 font-mono" value={judgeSecret} onChange={(e) => setJudgeSecret(e.target.value)} />
          </label>
        </div>
      )}
      {error && <p className="mt-3 max-w-3xl rounded border border-red-200 bg-red-50 p-2 text-red-800">{error}</p>}
      <button type="button" className="am-btn-primary mt-4" disabled={busy || !datasetId || selected.length === 0} onClick={() => void start()}>
        {busy ? "Starting…" : "Run evaluation"}
      </button>
    </div>
  );
}

// ---- Results ----------------------------------------------------------------------------------------
function SummaryTable({ ev }: { ev: EvaluationOut }) {
  if (!ev.summary) return null;
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-slate-400">
          <th className="py-1 pr-2 font-medium">Metric</th>
          <th className="py-1 pr-2 font-medium">Score</th>
          <th className="py-1 pr-2 font-medium">Cases</th>
          <th className="py-1 font-medium">How it is aggregated</th>
        </tr>
      </thead>
      <tbody>
        {ev.summary.map((m) => (
          <tr key={m.name} className="border-t border-slate-100">
            <td className="py-1 pr-2 text-slate-700">
              {m.title}
              {m.estimate && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-800">estimate</span>}
            </td>
            <td className="py-1 pr-2 font-mono font-medium text-slate-900">
              {formatMetric(m.mean, m.name, m.unit)}
              {m.p95 !== undefined && <span className="ml-1 text-slate-400">p95 {formatMetric(m.p95, m.name, m.unit)}</span>}
              {m.total !== undefined && m.name === "cost_usd" && <span className="ml-1 text-slate-400">total {formatMetric(m.total, m.name, m.unit)}</span>}
            </td>
            <td className="py-1 pr-2 text-slate-500">
              {m.n}/{m.of}
            </td>
            <td className="py-1 text-slate-400">{m.aggregation}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CasesTable({ cases, metrics }: { cases: EvalCaseResult[]; metrics: string[] }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <tr className="text-left text-[10px] uppercase tracking-wide text-slate-400">
          <th className="py-1 pr-2 font-medium">Case</th>
          {metrics.map((m) => (
            <th key={m} className="py-1 pr-2 font-medium">
              {m}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {cases.map((c) => (
          <Fragment key={c.case_id}>
            <tr className="cursor-pointer border-t border-slate-100 hover:bg-slate-50" onClick={() => setOpen(open === c.case_id ? null : c.case_id)}>
              <td className="py-1 pr-2 font-mono text-slate-700">
                {open === c.case_id ? "▾" : "▸"} {c.case_id}
                {c.status !== "succeeded" && <span className="ml-1 text-red-700">({c.status})</span>}
              </td>
              {metrics.map((name) => {
                const m = c.metrics.find((x) => x.name === name);
                return (
                  <td key={name} className={`py-1 pr-2 font-mono ${m?.passed === false ? "text-red-700" : m?.passed ? "text-emerald-700" : "text-slate-600"}`}>
                    {formatMetric(m?.value, name)}
                  </td>
                );
              })}
            </tr>
            {open === c.case_id && (
              <tr>
                <td colSpan={metrics.length + 1} className="bg-slate-50 p-2">
                  <div className="grid gap-2 md:grid-cols-2">
                    <div>
                      <div className="text-[10px] font-semibold uppercase text-slate-400">Answer</div>
                      <p className="whitespace-pre-wrap text-slate-700">{c.observation.answer ?? c.error?.message ?? "—"}</p>
                      <div className="mt-1 font-mono text-[10px] text-slate-400">run {c.run_id}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-semibold uppercase text-slate-400">
                        Retrieved{c.observation.retrieval_node ? ` (${c.observation.retrieval_node})` : ""}
                      </div>
                      <ol className="list-decimal pl-4 text-slate-600">
                        {c.observation.retrieved.slice(0, 8).map((d) => (
                          <li key={d.id}>{d.source?.section ?? d.source?.filename ?? d.id}</li>
                        ))}
                      </ol>
                    </div>
                  </div>
                  <details className="mt-1">
                    <summary className="cursor-pointer text-[11px] text-slate-500">Metric details</summary>
                    <pre className="mt-1 max-h-60 overflow-auto rounded bg-surface p-2 text-[10px] text-slate-600">
                      {JSON.stringify(c.metrics, null, 2)}
                    </pre>
                  </details>
                </td>
              </tr>
            )}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

function CompareView({ data }: { data: ComparisonOut }) {
  const name = (e: EvaluationOut) => e.label || `rev ${e.revision}`;
  return (
    <div className="space-y-3 text-xs">
      <div className="text-slate-600">
        <b>A</b> {name(data.a)} (rev {data.a.revision}, dataset v{data.a.dataset_version}) vs <b>B</b> {name(data.b)} (rev {data.b.revision},
        dataset v{data.b.dataset_version}) · <span className="text-red-700">{data.regressions} cases regressed</span> ·{" "}
        <span className="text-emerald-700">{data.improvements} improved</span>
      </div>
      {data.warnings.map((w) => (
        <p key={w} className="rounded border border-amber-200 bg-amber-50 p-2 text-amber-900">
          {w}
        </p>
      ))}
      <table className="w-full border-collapse">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-slate-400">
            <th className="py-1 pr-2 font-medium">Metric</th>
            <th className="py-1 pr-2 font-medium">A</th>
            <th className="py-1 pr-2 font-medium">B</th>
            <th className="py-1 font-medium">Change</th>
          </tr>
        </thead>
        <tbody>
          {data.metrics.map((m) => (
            <tr key={m.name} className="border-t border-slate-100">
              <td className="py-1 pr-2 text-slate-700">
                {m.title}
                {m.estimate && <span className="ml-1 rounded bg-amber-100 px-1 text-[10px] text-amber-800">estimate</span>}
              </td>
              <td className="py-1 pr-2 font-mono">{formatMetric(m.a, m.name, m.unit)}</td>
              <td className="py-1 pr-2 font-mono">{formatMetric(m.b, m.name, m.unit)}</td>
              <td className={`py-1 font-mono ${m.better === true ? "text-emerald-700" : m.better === false ? "text-red-700" : "text-slate-400"}`}>
                {formatDelta(m.delta, m.name, m.unit) || "no change"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.cases.some((c) => c.changes.length) && (
        <div>
          <h4 className="mb-1 font-semibold text-slate-700">Per-case changes</h4>
          <ul className="space-y-0.5">
            {data.cases
              .filter((c) => c.changes.length)
              .map((c) => (
                <li key={c.case_id}>
                  <span className="font-mono">{c.case_id}</span>:{" "}
                  {c.changes.map((ch) => (
                    <span key={ch.metric} className={`mr-2 ${ch.better ? "text-emerald-700" : "text-red-700"}`}>
                      {ch.metric} {formatMetric(ch.a, ch.metric)} → {formatMetric(ch.b, ch.metric)}
                    </span>
                  ))}
                </li>
              ))}
          </ul>
        </div>
      )}
      {(data.only_in_a.length > 0 || data.only_in_b.length > 0) && (
        <p className="text-slate-500">
          Not compared: only in A {data.only_in_a.join(", ") || "—"}; only in B {data.only_in_b.join(", ") || "—"}.
        </p>
      )}
      <p className="text-[11px] text-slate-400">{data.note}</p>
    </div>
  );
}

function ResultsTab({
  runs,
  datasets,
  focus,
  setFocus,
  reload,
  notify,
}: {
  runs: EvaluationOut[];
  datasets: DatasetOut[];
  focus: string | null;
  setFocus: (id: string | null) => void;
  reload: () => Promise<void>;
  notify: Props["notify"];
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [cases, setCases] = useState<EvalCaseResult[]>([]);
  const [comparison, setComparison] = useState<ComparisonOut | null>(null);
  const current = runs.find((r) => r.id === focus) ?? null;
  const dsName = (id: string) => datasets.find((d) => d.id === id)?.name ?? id;

  useEffect(() => {
    if (current?.status === "succeeded" || current?.status === "running") {
      void api.evaluationCases(current.id).then(setCases);
    } else setCases([]);
  }, [current?.id, current?.status, current?.done]);

  const togglePick = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  const runCompare = async (a: string, b: string) => {
    try {
      setComparison(await api.compareEvaluations(a, b));
      setFocus(null);
    } catch (err) {
      notify(errorText(err), "error");
    }
  };
  const baseline = runs.find((r) => r.baseline && current && r.dataset_id === current.dataset_id && r.id !== current.id);

  return (
    <div className="flex h-full min-h-0 gap-4">
      <aside className="w-72 shrink-0 overflow-y-auto">
        {picked.length === 2 && (
          <button type="button" className="am-btn-primary mb-2 w-full text-xs" onClick={() => void runCompare(picked[0]!, picked[1]!)}>
            Compare the two selected
          </button>
        )}
        {runs.length === 0 && <p className="text-xs text-slate-400">No evaluations yet.</p>}
        {runs.map((r) => (
          <div
            key={r.id}
            className={`mb-1 flex items-start gap-2 rounded-md border px-2 py-1.5 text-xs ${focus === r.id ? "border-accent-300 bg-accent-50" : "border-transparent hover:bg-slate-50"}`}
          >
            <input type="checkbox" className="mt-0.5" checked={picked.includes(r.id)} onChange={() => togglePick(r.id)} aria-label={`Select ${r.label || r.id} for comparison`} disabled={r.status !== "succeeded"} />
            <button type="button" className="min-w-0 flex-1 text-left" onClick={() => { setFocus(r.id); setComparison(null); }}>
              <span className="block truncate font-medium text-slate-800">
                {r.baseline && <span title="Baseline">★ </span>}
                {r.label || `rev ${r.revision}`}
              </span>
              <span className="block text-slate-500">
                rev {r.revision} · {dsName(r.dataset_id)} v{r.dataset_version} ·{" "}
                {r.status === "running" || r.status === "queued" ? `${r.done}/${r.total}` : r.status}
              </span>
            </button>
          </div>
        ))}
      </aside>
      <section className="min-w-0 flex-1 overflow-y-auto pr-1">
        {comparison ? (
          <CompareView data={comparison} />
        ) : !current ? (
          <p className="pt-2 text-sm text-slate-500">Select an evaluation, or tick two finished ones to compare them.</p>
        ) : (
          <div className="space-y-3 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold text-slate-800">{current.label || `rev ${current.revision}`}</h3>
              <span className="text-slate-500">
                rev {current.revision} · {dsName(current.dataset_id)} v{current.dataset_version} · {current.status}
                {current.setup && ` · answer from '${current.setup.answer_output}' · retrieval scored at '${current.setup.retrieval_node ?? "none"}' · K=${current.setup.k}`}
              </span>
              <div className="ml-auto flex gap-1.5">
                {(current.status === "running" || current.status === "queued") && (
                  <button type="button" className="am-btn !py-0.5 text-xs" onClick={async () => { await api.cancelEvaluation(current.id); await reload(); }}>
                    Cancel
                  </button>
                )}
                {current.status === "succeeded" && !current.baseline && (
                  <button type="button" className="am-btn !py-0.5 text-xs" onClick={async () => { await api.setBaseline(current.id); await reload(); notify("Baseline set", "success"); }}>
                    Set as baseline
                  </button>
                )}
                {current.status === "succeeded" && baseline?.status === "succeeded" && (
                  <button type="button" className="am-btn !py-0.5 text-xs" onClick={() => void runCompare(baseline.id, current.id)}>
                    Compare with baseline
                  </button>
                )}
                {current.status !== "running" && current.status !== "queued" && (
                  <button type="button" className="am-btn !py-0.5 text-xs text-red-700" onClick={async () => { await api.deleteEvaluation(current.id); setFocus(null); await reload(); }}>
                    Delete
                  </button>
                )}
              </div>
            </div>
            {current.error && <p className="rounded border border-red-200 bg-red-50 p-2 text-red-800">{current.error.message}</p>}
            {(current.status === "running" || current.status === "queued") && (
              <div className="h-1.5 w-full overflow-hidden rounded bg-slate-100">
                <div className="h-full bg-accent-500 transition-all" style={{ width: `${current.total ? (100 * current.done) / current.total : 0}%` }} />
              </div>
            )}
            <SummaryTable ev={current} />
            {current.setup?.judge && (
              <p className="text-[11px] text-amber-800">
                Judge: {current.setup.judge.model} ({Object.values(current.setup.judge.prompts).join(", ")}). {current.setup.judge.note}
              </p>
            )}
            {cases.length > 0 && <CasesTable cases={cases} metrics={current.config.evaluators.filter((e) => !["tokens", "retries"].includes(e))} />}
          </div>
        )}
      </section>
    </div>
  );
}

// ---- shell ------------------------------------------------------------------------------------------
export function EvaluationLab({ open, projectId, projectName, inputNodes, outputNodes, ensureSaved, onClose, notify }: Props) {
  const [tab, setTab] = useState<Tab>("datasets");
  const [datasets, setDatasets] = useState<DatasetOut[]>([]);
  const [runs, setRuns] = useState<EvaluationOut[]>([]);
  const [evaluators, setEvaluators] = useState<EvaluatorInfo[]>([]);
  const [focus, setFocus] = useState<string | null>(null);
  const inputKeys = useMemo(() => inputNodes.map((n) => String(n.config.key ?? n.id)), [inputNodes]);

  const reload = useCallback(async () => {
    if (!projectId) return;
    const [d, r] = await Promise.all([api.datasets(projectId), api.evaluations(projectId)]);
    setDatasets(d);
    setRuns(r);
  }, [projectId]);

  useEffect(() => {
    if (!open || !projectId) return;
    void reload();
    if (evaluators.length === 0) void api.evaluators().then(setEvaluators);
  }, [open, projectId, reload, evaluators.length]);

  const active = runs.some((r) => r.status === "running" || r.status === "queued");
  useEffect(() => {
    if (!open || !active) return;
    const t = window.setInterval(() => void reload(), 1500);
    return () => window.clearInterval(t);
  }, [open, active, reload]);

  if (!open || !projectId) return null;
  const tabs: { id: Tab; label: string }[] = [
    { id: "datasets", label: `Datasets (${datasets.length})` },
    { id: "run", label: "Run" },
    { id: "results", label: `Results (${runs.length})` },
  ];
  return (
    <div className="fixed inset-0 z-40 flex flex-col bg-surface" role="dialog" aria-modal="true" aria-label="Evaluation Lab">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b border-slate-200 px-4">
        <h2 className="text-sm font-semibold text-slate-900">Evaluation Lab</h2>
        <span className="text-xs text-slate-500">{projectName}</span>
        <nav className="ml-4 flex gap-1" role="tablist">
          {tabs.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
              className={`rounded-md px-3 py-1 text-xs ${tab === t.id ? "bg-accent-100 font-medium text-accent-800" : "text-slate-600 hover:bg-slate-100"}`}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <button type="button" className="am-btn ml-auto text-xs" onClick={onClose}>
          Close
        </button>
      </header>
      <main className="min-h-0 flex-1 p-4">
        {tab === "datasets" && <DatasetsTab projectId={projectId} inputKeys={inputKeys} datasets={datasets} reload={reload} notify={notify} />}
        {tab === "run" && (
          <RunTab
            projectId={projectId}
            datasets={datasets}
            evaluators={evaluators}
            outputNodes={outputNodes}
            ensureSaved={ensureSaved}
            onStarted={(ev) => {
              setFocus(ev.id);
              setTab("results");
              void reload();
            }}
          />
        )}
        {tab === "results" && <ResultsTab runs={runs} datasets={datasets} focus={focus} setFocus={setFocus} reload={reload} notify={notify} />}
      </main>
    </div>
  );
}

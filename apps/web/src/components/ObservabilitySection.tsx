import { useCallback, useEffect, useState, type FormEvent } from "react";

import { observability as obsApi, type ObservabilityForm, type ObservabilityState } from "../api/client";
import { errorMessage, Field } from "./AuthGate";

const PRESETS: { label: string; endpoint: string; protocol: "http/protobuf" | "http/json"; hint: string }[] = [
  { label: "OTel Collector / Jaeger / Tempo", endpoint: "http://localhost:4318", protocol: "http/protobuf", hint: "" },
  {
    label: "Langfuse Cloud (EU)",
    endpoint: "https://cloud.langfuse.com/api/public/otel",
    protocol: "http/protobuf",
    hint: 'Headers secret value: Authorization=Basic <base64 of pk-lf-…:sk-lf-…>,x-langfuse-ingestion-version=4',
  },
  { label: "Arize Phoenix (local)", endpoint: "http://localhost:6006", protocol: "http/protobuf", hint: "" },
];

/** Settings → Observability: where finished runs are exported as OpenTelemetry traces (ADR-0037). */
export function ObservabilitySection({ notify }: { notify: (text: string, tone?: "info" | "error" | "success") => void }) {
  const [state, setState] = useState<ObservabilityState | null>(null);
  const [form, setForm] = useState<ObservabilityForm>({
    enabled: true, endpoint: "", protocol: "http/protobuf", headers_secret: "", service_name: "agenticmind", include_content: false,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const apply = useCallback((s: ObservabilityState) => {
    setState(s);
    if (s.settings) setForm({ ...s.settings, headers_secret: s.settings.headers_secret ?? "" });
  }, []);
  useEffect(() => {
    obsApi.get().then(apply, (err) => setError(errorMessage(err)));
  }, [apply]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      apply(await obsApi.save({ ...form, headers_secret: form.headers_secret || null }));
      notify("Observability settings saved", "success");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const test = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await obsApi.test();
      notify(`Test span sent to ${r.url}`, "success");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const reset = async () => {
    apply(await obsApi.reset());
    notify("Using the server's OTEL_* settings", "info");
  };

  if (!state) return <p className="text-sm text-slate-500">{error ?? "Loading…"}</p>;
  const ro = !state.can_edit;
  const s = state.status;
  return (
    <div className="space-y-4 text-sm">
      <p className="text-slate-600">
        Every finished run is exported as an OpenTelemetry trace (run, node, model and tool-call spans with GenAI
        attributes) to any OTLP endpoint: an OTel Collector, Jaeger, Grafana Tempo, Honeycomb, Datadog, Langfuse or
        Arize Phoenix. Prometheus can scrape <code className="font-mono">/api/metrics</code>.
      </p>
      <div className="rounded border border-slate-200 p-3 text-xs">
        <div className="font-medium text-slate-800">
          Now: {state.source === "off" ? "not exporting" : `exporting (${state.source === "workspace" ? "these settings" : "server OTEL_* variables"})`}
        </div>
        {state.effective && (
          <div className="mt-1 font-mono text-slate-600">
            {state.effective.url} · {state.effective.protocol}
            {state.effective.header_names.length > 0 && ` · headers: ${state.effective.header_names.join(", ")}`}
          </div>
        )}
        <div className="mt-1 text-slate-600">
          Exported runs: {s.exported} · failed: {s.failed}
          {s.last_export_at && ` · last: ${new Date(s.last_export_at).toLocaleString()}`}
        </div>
        {s.last_error && <div className="mt-1 break-words text-rose-700">Last error: {s.last_error}</div>}
      </div>
      <form onSubmit={save} className="space-y-3">
        <fieldset disabled={ro || busy} className="space-y-3">
          <div className="flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <button key={p.label} type="button" className="am-btn !py-1 text-xs" title={p.hint}
                onClick={() => setForm({ ...form, endpoint: p.endpoint, protocol: p.protocol })}>
                {p.label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-xs text-slate-700">
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
            Export traces
          </label>
          <Field label="OTLP endpoint" hint="Base URL; /v1/traces is added (e.g. http://localhost:4318).">
            <input className="am-input font-mono text-[13px]" value={form.endpoint} placeholder="http://localhost:4318"
              onChange={(e) => setForm({ ...form, endpoint: e.target.value })} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Protocol">
              <select className="am-input" value={form.protocol}
                onChange={(e) => setForm({ ...form, protocol: e.target.value as ObservabilityForm["protocol"] })}>
                <option value="http/protobuf">http/protobuf</option>
                <option value="http/json">http/json</option>
              </select>
            </Field>
            <Field label="Service name">
              <input className="am-input" value={form.service_name} onChange={(e) => setForm({ ...form, service_name: e.target.value })} />
            </Field>
          </div>
          <Field label="Headers secret (optional)" hint="Name of a secret whose value is key=value,key=value headers, e.g. an Authorization header. Values are never shown.">
            <input className="am-input font-mono text-[13px]" value={form.headers_secret ?? ""} placeholder="OTEL_HEADERS"
              onChange={(e) => setForm({ ...form, headers_secret: e.target.value.toUpperCase() })} />
          </Field>
          <label className="flex items-center gap-2 text-xs text-slate-700">
            <input type="checkbox" checked={form.include_content}
              onChange={(e) => setForm({ ...form, include_content: e.target.checked })} />
            Include inputs and outputs (prompts, answers, tool arguments): may contain personal data
          </label>
        </fieldset>
        {error && <p className="text-xs text-rose-700">{error}</p>}
        {!ro && (
          <div className="flex gap-2">
            <button className="am-btn-primary" type="submit" disabled={busy}>Save</button>
            <button className="am-btn" type="button" disabled={busy || state.source === "off"} onClick={() => void test()}>
              Send test span
            </button>
            {state.settings && (
              <button className="am-btn" type="button" disabled={busy} onClick={() => void reset()}>
                Use server settings
              </button>
            )}
          </div>
        )}
      </form>
    </div>
  );
}

import { useEffect, useState } from "react";

import { secrets as secretsApi } from "../api/client";
import type { SecretSource } from "../api/types";

import type { JsonValue, Mode, NodeManifest } from "../api/types";
import { fieldsFromSchema, fieldVisible, type FieldSpec } from "../lib/schemaForm";
import { ArtifactsControl } from "./Files";
import { RouteRefControl, RoutesControl } from "./RoutesControl";

interface Props {
  manifest: NodeManifest;
  config: Record<string, JsonValue>;
  mode: Mode;
  errorsByField: Map<string, string[]>;
  onChange: (config: Record<string, JsonValue>) => void;
}

export function ConfigForm({ manifest, config, mode, errorsByField, onChange }: Props) {
  const fields = fieldsFromSchema(manifest.config_schema);
  const applicable = fields.filter((f) => fieldVisible(f, config, fields));
  const visible = applicable.filter((f) => mode !== "no_code" || !f.advanced);
  const hidden = applicable.length - visible.length;

  const set = (name: string, value: JsonValue | undefined) => {
    const next = { ...config };
    if (value === undefined) delete next[name];
    else next[name] = value;
    onChange(next);
  };

  if (fields.length === 0) return <p className="text-xs text-slate-500">This node has no configuration.</p>;

  return (
    <div className="space-y-3">
      {visible.map((f) => (
        <Field
          key={`${manifest.type}:${f.name}`}
          spec={f}
          value={config[f.name]}
          siblings={(name) => (config[name] !== undefined ? config[name] : (fields.find((x) => x.name === name)?.default as JsonValue | undefined))}
          errors={errorsByField.get(f.name)}
          onChange={(v) => set(f.name, v)}
        />
      ))}
      {hidden > 0 && (
        <p className="text-[11px] text-slate-400">
          {hidden} advanced field{hidden > 1 ? "s" : ""} hidden in no-code mode.
        </p>
      )}
    </div>
  );
}

function Field({
  spec,
  value,
  siblings,
  errors,
  onChange,
}: {
  spec: FieldSpec;
  value: JsonValue | undefined;
  /** Another field's effective value (e.g. the routes a default_route picks from). */
  siblings: (name: string) => JsonValue | undefined;
  errors?: string[];
  onChange: (v: JsonValue | undefined) => void;
}) {
  const effective = value !== undefined ? value : (spec.default as JsonValue | undefined);
  const id = `cfg-${spec.name}`;
  return (
    <div>
      <label htmlFor={id} className="mb-1 flex items-center justify-between text-xs font-medium text-slate-700">
        <span>
          {spec.title}
          {spec.required && <span className="text-red-500"> *</span>}
        </span>
        {spec.advanced && <span className="text-[10px] font-normal uppercase text-slate-400">advanced</span>}
      </label>
      <Control id={id} spec={spec} value={effective} siblings={siblings} onChange={onChange} />
      {spec.description && <p className="mt-1 text-[11px] leading-snug text-slate-500">{spec.description}</p>}
      {errors?.map((e) => (
        <p key={e} className="mt-1 text-[11px] text-red-600">
          {e}
        </p>
      ))}
    </div>
  );
}

function Control({
  id,
  spec,
  value,
  siblings,
  onChange,
}: {
  id: string;
  spec: FieldSpec;
  value: JsonValue | undefined;
  siblings: (name: string) => JsonValue | undefined;
  onChange: (v: JsonValue | undefined) => void;
}) {
  switch (spec.kind) {
    case "enum":
      return (
        <select id={id} className="am-input" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
          {spec.options?.map((o) => (
            <option key={String(o)} value={String(o)}>
              {String(o)}
            </option>
          ))}
        </select>
      );
    case "boolean":
      return (
        <input
          id={id}
          type="checkbox"
          className="h-4 w-4 accent-accent-600"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
      );
    case "integer":
    case "number":
      return (
        <input
          id={id}
          type="number"
          className="am-input"
          value={typeof value === "number" ? value : ""}
          min={spec.minimum}
          max={spec.maximum}
          step={spec.kind === "integer" ? 1 : "any"}
          onChange={(e) => {
            if (e.target.value === "") return onChange(undefined);
            const n = spec.kind === "integer" ? parseInt(e.target.value, 10) : parseFloat(e.target.value);
            if (!Number.isNaN(n)) onChange(n);
          }}
        />
      );
    case "textarea":
      return (
        <textarea
          id={id}
          className="am-input min-h-[80px] font-mono text-xs"
          value={typeof value === "string" ? value : ""}
          placeholder={spec.placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case "routes":
      return <RoutesControl value={value} onChange={onChange} />;
    case "route_ref":
      return <RouteRefControl id={id} value={value} routes={siblings("routes")} onChange={onChange} />;
    case "artifacts":
      return <ArtifactsControl value={value} onChange={(ids) => onChange(ids)} />;
    case "secret":
      return <SecretControl id={id} value={value} onChange={onChange} />;
    case "json":
      return <JsonControl id={id} value={value} onChange={onChange} />;
    default:
      return (
        <input
          id={id}
          className="am-input"
          value={typeof value === "string" ? value : ""}
          placeholder={spec.placeholder}
          onChange={(e) => onChange(e.target.value)}
        />
      );
  }
}

function SecretControl({ id, value, onChange }: { id: string; value: JsonValue | undefined; onChange: (v: JsonValue | undefined) => void }) {
  const name =
    value && typeof value === "object" && !Array.isArray(value) && typeof value["$secret"] === "string"
      ? value["$secret"]
      : "";
  const literal = typeof value === "string";
  return (
    <div>
      <div className="flex items-center gap-1">
        <span className="rounded bg-slate-100 px-1.5 py-1 font-mono text-[10px] text-slate-600">$secret</span>
        <input
          id={id}
          className="am-input font-mono"
          placeholder="ENV_VAR_NAME"
          value={name}
          onChange={(e) => {
            const v = e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "");
            onChange(v ? { $secret: v } : undefined);
          }}
        />
      </div>
      <SecretStatus name={name} />
      {literal && <p className="mt-1 text-[11px] text-red-600">A literal value is stored here. Replace it with a reference.</p>}
    </div>
  );
}

function JsonControl({ id, value, onChange }: { id: string; value: JsonValue | undefined; onChange: (v: JsonValue | undefined) => void }) {
  const serialized = value === undefined ? "" : JSON.stringify(value, null, 2);
  const [text, setText] = useState(serialized);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    setText(serialized);
    setError(null);
  }, [serialized]);
  return (
    <div>
      <textarea
        id={id}
        className="am-input min-h-[56px] font-mono text-xs"
        value={text}
        placeholder="JSON value, e.g. 42, &quot;text&quot;, {&quot;a&quot;: 1}"
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          if (text.trim() === "") {
            setError(null);
            return onChange(undefined);
          }
          try {
            onChange(JSON.parse(text) as JsonValue);
            setError(null);
          } catch {
            setError("Not valid JSON. Strings need quotes.");
          }
        }}
      />
      {error && <p className="mt-1 text-[11px] text-red-600">{error}</p>}
    </div>
  );
}

/** Whether a secret name resolves on the server (Settings → Secrets or the environment). Never the value. */
const statusCache = new Map<string, SecretSource>();

function SecretStatus({ name }: { name: string }) {
  const [status, setStatus] = useState<SecretSource | undefined>(() => (name ? statusCache.get(name) : undefined));
  useEffect(() => {
    if (!name) return;
    if (statusCache.has(name)) {
      setStatus(statusCache.get(name));
      return;
    }
    setStatus(undefined);
    const timer = window.setTimeout(() => {
      secretsApi.status([name]).then(
        (r) => {
          statusCache.set(name, r[name] ?? null);
          setStatus(r[name] ?? null);
        },
        () => undefined,
      );
    }, 350);
    return () => window.clearTimeout(timer);
  }, [name]);
  if (!name) {
    return <p className="mt-1 text-[11px] text-slate-500">The name of a secret (Settings → Secrets or the server environment). Values never enter the project.</p>;
  }
  if (status === undefined) return <p className="mt-1 text-[11px] text-slate-400">Checking…</p>;
  if (status === null) {
    return (
      <p className="mt-1 text-[11px] text-amber-700">
        Not set yet. Add <span className="font-mono">{name}</span> in avatar → Settings → Secrets (or the server’s .env).
      </p>
    );
  }
  return (
    <p className="mt-1 text-[11px] text-emerald-700">
      ✓ Set {status === "workspace" ? "in Settings → Secrets" : "in the server environment"}. The value never enters the project.
    </p>
  );
}

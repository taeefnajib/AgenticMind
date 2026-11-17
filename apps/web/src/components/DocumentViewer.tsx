import { useState } from "react";

import type { DocumentValue } from "../api/types";

export function isDocument(v: unknown): v is DocumentValue {
  return (
    typeof v === "object" &&
    v !== null &&
    typeof (v as DocumentValue).id === "string" &&
    typeof (v as DocumentValue).content === "string" &&
    typeof (v as DocumentValue).source === "object"
  );
}

export function isDocumentList(v: unknown): v is DocumentValue[] {
  return Array.isArray(v) && v.length > 0 && v.every(isDocument);
}

/** Content without a leading Markdown heading line (the section path already shows it). */
function preview(d: DocumentValue): string {
  if (!d.source.section) return d.content;
  const stripped = d.content.replace(/^#{1,6}[ \t].*(\r?\n)+/, "");
  return stripped || d.content;
}

function sourceLabel(d: DocumentValue): string {
  const parts = [d.source.filename ?? d.source.uri];
  if (d.source.page) parts.push(`p. ${d.source.page}`);
  if (d.source.section) parts.push(d.source.section);
  return parts.join(" · ");
}

/** Readable list of documents/chunks with provenance, used for DocumentSet outputs. */
export function DocumentViewer({ docs, limit = 50 }: { docs: DocumentValue[]; limit?: number }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const visible = showAll ? docs : docs.slice(0, limit);
  const totalChars = docs.reduce((n, d) => n + d.content.length, 0);

  return (
    <div>
      <div className="mb-1.5 text-[11px] text-slate-500">
        {docs.length} document{docs.length === 1 ? "" : "s"} · {totalChars.toLocaleString()} characters
      </div>
      <ul className="space-y-1.5">
        {visible.map((d) => {
          const open = expanded === d.id;
          return (
            <li key={d.id} className="rounded-md border border-slate-200 bg-white">
              <button
                type="button"
                className="w-full px-2 py-1.5 text-left"
                onClick={() => setExpanded(open ? null : d.id)}
                aria-expanded={open}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-[11px] font-medium text-slate-700">{sourceLabel(d)}</span>
                  <span className="flex shrink-0 items-center gap-2 font-mono text-[10px] text-slate-400">
                    {d.score !== undefined && (
                      <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-semibold text-indigo-700" title="Similarity score">
                        {d.score.toFixed(3)}
                      </span>
                    )}
                    {d.offsets ? `${d.offsets.start}–${d.offsets.end}` : `${d.content.length} ch`}
                  </span>
                </div>
                <div className={`mt-0.5 whitespace-pre-wrap text-xs text-slate-800 ${open ? "" : "line-clamp-2"}`}>
                  {open ? d.content : preview(d)}
                </div>
              </button>
              {open && (
                <dl className="grid grid-cols-[80px_1fr] gap-x-2 gap-y-0.5 border-t border-slate-100 px-2 py-1.5 font-mono text-[10px] text-slate-600">
                  <dt className="text-slate-400">id</dt>
                  <dd className="break-all">{d.id}</dd>
                  {d.parent_id && (
                    <>
                      <dt className="text-slate-400">parent</dt>
                      <dd className="break-all">{d.parent_id}</dd>
                    </>
                  )}
                  {d.score !== undefined && (
                    <>
                      <dt className="text-slate-400">score</dt>
                      <dd>{d.score.toFixed(4)}</dd>
                    </>
                  )}
                  {d.ingestion_version && (
                    <>
                      <dt className="text-slate-400">pipeline</dt>
                      <dd className="break-all">{d.ingestion_version}</dd>
                    </>
                  )}
                  {d.metadata && Object.keys(d.metadata).length > 0 && (
                    <>
                      <dt className="text-slate-400">metadata</dt>
                      <dd className="break-all">{JSON.stringify(d.metadata)}</dd>
                    </>
                  )}
                </dl>
              )}
            </li>
          );
        })}
      </ul>
      {docs.length > limit && !showAll && (
        <button type="button" className="mt-1.5 text-xs text-sky-700 underline" onClick={() => setShowAll(true)}>
          Show all {docs.length}
        </button>
      )}
    </div>
  );
}

/** Renders any output value: documents get the viewer, everything else pretty JSON. */
export function ValueView({ value }: { value: unknown }) {
  if (isDocumentList(value)) return <DocumentViewer docs={value} />;
  if (isDocument(value)) return <DocumentViewer docs={[value]} />;
  return (
    <pre className="max-h-72 overflow-auto rounded bg-slate-50 p-2 font-mono text-[11px] text-slate-800">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

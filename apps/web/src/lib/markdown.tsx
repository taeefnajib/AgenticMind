// Minimal, safe Markdown for model answers: paragraphs, bullet/numbered lists, headings, **bold**,
// *italic*, `code` and [n] citation markers. Builds React elements only (never innerHTML), so model
// output cannot inject markup or scripts.

import type { ReactNode } from "react";

const INLINE = /(\*\*[^*\n]+\*\*|`[^`\n]+`|\*[^*\s\n][^*\n]*\*|_[^_\s\n][^_\n]*_|\[\d+\])/g;

export type CitationRenderer = (n: number, key: string) => ReactNode;

export function renderInline(text: string, cite?: CitationRenderer, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  for (const m of text.matchAll(INLINE)) {
    const start = m.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const tok = m[0];
    const key = `${keyPrefix}-${k++}`;
    if (tok.startsWith("**")) out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    else if (tok.startsWith("`")) {
      out.push(
        <code key={key} className="rounded bg-slate-100 px-1 font-mono text-[0.85em]">
          {tok.slice(1, -1)}
        </code>,
      );
    } else if (tok.startsWith("[")) out.push(cite ? cite(Number(tok.slice(1, -1)), key) : tok);
    else out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    last = start + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Block =
  | { kind: "p"; lines: string[] }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "h"; text: string };

export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  for (const raw of text.replace(/\r\n/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    const bullet = /^\s*[-*•]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const heading = /^\s*#{1,6}\s+(.*)$/.exec(line);
    const prev = blocks[blocks.length - 1];
    if (!line.trim()) {
      blocks.push({ kind: "p", lines: [] });
    } else if (bullet) {
      if (prev?.kind === "ul") prev.items.push(bullet[1] ?? "");
      else blocks.push({ kind: "ul", items: [bullet[1] ?? ""] });
    } else if (numbered) {
      if (prev?.kind === "ol") prev.items.push(numbered[1] ?? "");
      else blocks.push({ kind: "ol", items: [numbered[1] ?? ""] });
    } else if (heading) {
      blocks.push({ kind: "h", text: heading[1] ?? "" });
    } else if (prev?.kind === "p") {
      prev.lines.push(line);
    } else {
      blocks.push({ kind: "p", lines: [line] });
    }
  }
  return blocks.filter((b) => (b.kind === "p" ? b.lines.length > 0 : true));
}

export function Markdown({ text, cite }: { text: string; cite?: CitationRenderer }) {
  return (
    <div className="space-y-2 text-sm leading-relaxed text-slate-800">
      {parseBlocks(text).map((b, i) => {
        if (b.kind === "h") {
          return (
            <div key={i} className="font-semibold text-slate-900">
              {renderInline(b.text, cite, `h${i}`)}
            </div>
          );
        }
        if (b.kind === "ul" || b.kind === "ol") {
          const List = b.kind;
          return (
            <List key={i} className={`space-y-1 pl-5 ${b.kind === "ul" ? "list-disc" : "list-decimal"}`}>
              {b.items.map((item, j) => (
                <li key={j}>{renderInline(item, cite, `l${i}-${j}`)}</li>
              ))}
            </List>
          );
        }
        if (b.kind !== "p") return null;
        return (
          <p key={i}>
            {b.lines.map((line, j) => (
              <span key={j}>
                {j > 0 && <br />}
                {renderInline(line, cite, `p${i}-${j}`)}
              </span>
            ))}
          </p>
        );
      })}
    </div>
  );
}

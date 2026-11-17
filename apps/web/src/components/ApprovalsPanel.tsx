import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type { JsonValue, WaitOut } from "../api/types";

export interface ApprovalResponse {
  decision: "approved" | "rejected";
  comment?: string;
  reviewer?: string;
  value?: JsonValue;
}

/** Build the response for a Human Approval request; returns an error message when it is incomplete. */
export function buildResponse(
  wait: WaitOut,
  decision: "approved" | "rejected",
  form: { comment: string; reviewer: string; edited: string },
): ApprovalResponse | string {
  const req = wait.request as { allow_edit?: boolean; require_comment_on_reject?: boolean; value?: JsonValue };
  const response: ApprovalResponse = { decision };
  if (form.comment.trim()) response.comment = form.comment.trim();
  if (form.reviewer.trim()) response.reviewer = form.reviewer.trim();
  if (decision === "rejected" && req.require_comment_on_reject && !response.comment) {
    return "Please explain why you are rejecting.";
  }
  if (decision === "approved" && req.allow_edit) {
    const original = req.value;
    if (typeof original === "string") {
      if (form.edited !== original) response.value = form.edited;
    } else {
      const before = JSON.stringify(original, null, 2);
      if (form.edited !== before) {
        try {
          response.value = JSON.parse(form.edited) as JsonValue;
        } catch {
          return "The edited value is not valid JSON.";
        }
      }
    }
  }
  return response;
}

function initialText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function ApprovalCard({ wait, onRespond }: { wait: WaitOut; onRespond: (r: ApprovalResponse) => Promise<void> }) {
  const req = wait.request as {
    title?: string;
    instructions?: string;
    value?: unknown;
    allow_edit?: boolean;
  };
  const [edited, setEdited] = useState(initialText(req.value));
  const [comment, setComment] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (decision: "approved" | "rejected") => {
    const built = buildResponse(wait, decision, { comment, reviewer, edited });
    if (typeof built === "string") {
      setError(built);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onRespond(built);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="rounded-lg border border-slate-200 p-3">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-semibold text-slate-800">{req.title || "Approval needed"}</h3>
        <span className="font-mono text-[10px] text-slate-400">
          {wait.node_id} · {wait.run_id} · {wait.created_at ? new Date(wait.created_at).toLocaleString() : ""}
        </span>
      </div>
      {req.instructions && <p className="mt-0.5 text-xs text-slate-500">{req.instructions}</p>}
      <label className="mt-2 block text-[11px] font-medium text-slate-500">
        {req.allow_edit ? "Content (you can edit it before approving)" : "Content"}
        <textarea
          className="am-input mt-1 min-h-24 font-sans text-sm"
          value={edited}
          readOnly={!req.allow_edit}
          onChange={(e) => setEdited(e.target.value)}
        />
      </label>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <label className="min-w-48 flex-1 text-[11px] font-medium text-slate-500">
          Comment
          <input className="am-input mt-1 text-xs" value={comment} onChange={(e) => setComment(e.target.value)} />
        </label>
        <label className="w-40 text-[11px] font-medium text-slate-500">
          Your name (optional)
          <input className="am-input mt-1 text-xs" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
        </label>
        <button type="button" className="am-btn" disabled={busy} onClick={() => void submit("rejected")}>
          Reject
        </button>
        <button type="button" className="am-btn-primary" disabled={busy} onClick={() => void submit("approved")}>
          Approve
        </button>
      </div>
      {error && <p className="mt-1 text-xs text-red-700">{error}</p>}
    </li>
  );
}

/** The project's approval inbox: runs paused at Human Approval nodes (ADR-0013). */
export function ApprovalsPanel({
  projectId,
  refreshKey,
  onResume,
  onCount,
}: {
  projectId?: string;
  refreshKey?: unknown;
  onResume: (runId: string, responses: Record<string, JsonValue>) => Promise<void>;
  onCount?: (n: number) => void;
}) {
  const [waits, setWaits] = useState<WaitOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    try {
      const list = await api.approvals(projectId);
      setWaits(list);
      onCount?.(list.length);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId, onCount]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  if (!projectId) return null;
  return (
    <div className="text-xs">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-slate-500">
          Runs paused at a <b>Human Approval</b> step. Nothing is running while they wait; your answer resumes the run
          from its checkpoint.
        </span>
        <button type="button" className="ml-auto text-slate-500 hover:text-slate-800" onClick={() => void load()}>
          Refresh
        </button>
      </div>
      {error && <p className="mb-2 text-red-700">{error}</p>}
      {waits.length === 0 ? (
        <p className="text-slate-400">No approvals waiting.</p>
      ) : (
        <ul className="space-y-2">
          {waits.map((w) => (
            <ApprovalCard
              key={w.id}
              wait={w}
              onRespond={async (response) => {
                await onResume(w.run_id, { [w.node_id]: response as unknown as JsonValue });
                await load();
              }}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import type { ConversationSummary, ConversationTurn, MemoryRecord } from "../api/types";

function when(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Everything the project remembers (ADR-0012): long-term memories and stored conversations, deletable. */
export function MemoryPanel({ projectId, refreshKey }: { projectId?: string; refreshKey?: unknown }) {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [open, setOpen] = useState<{ id: string; turns: ConversationTurn[] } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!projectId) return;
    try {
      const [m, c] = await Promise.all([api.memories(projectId), api.conversations(projectId)]);
      setMemories(m);
      setConversations(c);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  if (!projectId) return null;

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
    } catch (err) {
      setError((err as Error).message);
    }
    await load();
  };

  return (
    <div className="grid gap-4 text-xs md:grid-cols-2">
      <section>
        <div className="mb-1.5 flex items-center gap-2">
          <h3 className="font-semibold text-slate-700">Long-term memories</h3>
          <span className="text-slate-400">{memories.length}</span>
          <button type="button" className="ml-auto text-slate-500 hover:text-slate-800" onClick={() => void load()}>
            Refresh
          </button>
          {memories.length > 0 && (
            <button
              type="button"
              className="text-red-600 hover:text-red-800"
              onClick={() => void act(() => api.clearMemories(projectId))}
            >
              Delete all
            </button>
          )}
        </div>
        {error && <p className="mb-1 text-red-700">{error}</p>}
        {memories.length === 0 ? (
          <p className="text-slate-400">
            Nothing stored. Memories are saved only by Remember / Save memory steps, and only with consent.
          </p>
        ) : (
          <ul className="space-y-1">
            {memories.map((m) => (
              <li key={m.id} className="flex items-start gap-2 rounded border border-slate-200 px-2 py-1.5">
                <div className="min-w-0 flex-1">
                  <div className="text-slate-800">{m.content}</div>
                  <div className="mt-0.5 text-[10px] text-slate-400">
                    {m.namespace} · saved {when(m.created_at)}
                    {m.expires_at ? ` · expires ${when(m.expires_at)}` : " · kept until deleted"}
                  </div>
                </div>
                <button
                  type="button"
                  className="shrink-0 text-slate-400 hover:text-red-700"
                  aria-label={`Delete memory: ${m.content}`}
                  onClick={() => void act(() => api.deleteMemory(projectId, m.id))}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section>
        <div className="mb-1.5 flex items-center gap-2">
          <h3 className="font-semibold text-slate-700">Conversations</h3>
          <span className="text-slate-400">{conversations.length}</span>
        </div>
        {conversations.length === 0 ? (
          <p className="text-slate-400">No stored conversations. Save Conversation Turn steps add them.</p>
        ) : (
          <ul className="space-y-1">
            {conversations.map((c) => (
              <li key={c.id} className="rounded border border-slate-200 px-2 py-1.5">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="min-w-0 flex-1 truncate text-left font-mono text-slate-700 hover:text-accent-700"
                    onClick={async () =>
                      setOpen(open?.id === c.id ? null : { id: c.id, turns: await api.conversation(projectId, c.id) })
                    }
                  >
                    {c.id}
                  </button>
                  <span className="text-slate-400">
                    {c.messages} msgs · {when(c.updated_at)}
                  </span>
                  <button
                    type="button"
                    className="text-slate-400 hover:text-red-700"
                    aria-label={`Delete conversation ${c.id}`}
                    onClick={() =>
                      void act(async () => {
                        await api.deleteConversation(projectId, c.id);
                        if (open?.id === c.id) setOpen(null);
                      })
                    }
                  >
                    ✕
                  </button>
                </div>
                {open?.id === c.id && (
                  <ol className="mt-1.5 space-y-1 border-t border-slate-100 pt-1.5">
                    {open.turns.map((t, i) => (
                      <li key={i}>
                        <span className={t.role === "user" ? "font-semibold text-accent-700" : "font-semibold text-slate-600"}>
                          {t.role}:
                        </span>{" "}
                        <span className="whitespace-pre-wrap text-slate-700">{t.content}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

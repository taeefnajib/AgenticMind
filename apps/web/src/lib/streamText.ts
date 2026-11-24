/** Live text from a run's ``token`` events (ADR-0040): accumulated per node, deduplicated by chunk seq. */

export interface TokenEvent {
  seq: number;
  node_id: string;
  invocation_id: string;
  text: string;
}

export interface LiveText {
  lastSeq: number;
  byNode: Record<string, string>;
  /** The node that streamed most recently (the one to show in chat). */
  latest: string | null;
}

export const EMPTY_LIVE: LiveText = { lastSeq: 0, byNode: {}, latest: null };

export function applyToken(state: LiveText, token: TokenEvent): LiveText {
  if (token.seq <= state.lastSeq) return state; // replayed after a reconnect
  return {
    lastSeq: token.seq,
    byNode: { ...state.byNode, [token.node_id]: (state.byNode[token.node_id] ?? "") + token.text },
    latest: token.node_id,
  };
}

export function latestText(state: LiveText): string {
  return state.latest ? state.byNode[state.latest] ?? "" : "";
}

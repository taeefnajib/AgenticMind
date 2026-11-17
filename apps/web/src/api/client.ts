import type {
  NodeManifest,
  ProjectDocument,
  ProjectOut,
  ProjectSummary,
  RunOut,
  TemplateInfo,
  TraceEvent,
  ValidationResult,
  ArtifactInfo,
  Diagnostic,
  JsonValue,
} from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "string" ? detail : ApiError.describe(status, detail));
  }

  static describe(status: number, detail: unknown): string {
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message: unknown }).message);
    }
    return `Request failed (${status})`;
  }

  /** Diagnostics carried by 422 responses from save / run. */
  get diagnostics(): Diagnostic[] {
    if (this.detail && typeof this.detail === "object" && "diagnostics" in this.detail) {
      return (this.detail as { diagnostics: Diagnostic[] }).diagnostics;
    }
    return [];
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail: unknown = undefined;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body) });

export const api = {
  nodes: () => request<NodeManifest[]>("/nodes"),
  templates: () => request<TemplateInfo[]>("/templates"),
  projects: () => request<ProjectSummary[]>("/projects"),
  createProject: (body: { name: string; description?: string; template?: string; document?: unknown }) =>
    request<ProjectOut>("/projects", { method: "POST", ...json(body) }),
  getProject: (id: string) => request<ProjectOut>(`/projects/${id}`),
  saveProject: (id: string, document: ProjectDocument, expectedRevision?: number) =>
    request<ProjectOut>(`/projects/${id}`, {
      method: "PUT",
      ...json({ document, expected_revision: expectedRevision }),
    }),
  validate: (id: string, document?: ProjectDocument) =>
    request<ValidationResult>(`/projects/${id}/validate`, { method: "POST", ...json({ document }) }),
  startRun: (id: string, inputs: Record<string, JsonValue>, targets?: string[]) =>
    request<RunOut>(`/projects/${id}/runs`, { method: "POST", ...json({ inputs, targets }) }),
  getRun: (runId: string) => request<RunOut>(`/runs/${runId}`),
  cancelRun: (runId: string) => request<RunOut>(`/runs/${runId}/cancel`, { method: "POST" }),
  events: (runId: string) => request<TraceEvent[]>(`/runs/${runId}/events`),
  nodeOutputs: (runId: string, nodeId: string) =>
    request<{ outputs: Record<string, JsonValue> }>(`/runs/${runId}/nodes/${nodeId}/outputs`),
  files: (projectId: string) => request<ArtifactInfo[]>(`/projects/${projectId}/files`),
  fileTypes: (projectId: string) =>
    request<{ extensions: string[]; max_bytes: number }>(`/projects/${projectId}/files/types`),
  async uploadFile(projectId: string, file: File): Promise<ArtifactInfo> {
    const body = new FormData();
    body.append("file", file, file.name);
    // no JSON content-type: the browser sets the multipart boundary
    const res = await fetch(`/api/projects/${projectId}/files`, { method: "POST", body });
    if (!res.ok) {
      let detail: unknown = undefined;
      try {
        detail = (await res.json()).detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as ArtifactInfo;
  },
  deleteFile: (projectId: string, fileId: string) =>
    request<void>(`/projects/${projectId}/files/${fileId}`, { method: "DELETE" }),
  fileUrl: (projectId: string, fileId: string) => `/api/projects/${projectId}/files/${fileId}/content`,
  async exportIR(id: string): Promise<{ blob: Blob; filename: string }> {
    const res = await fetch(`/api/projects/${id}/export`);
    if (!res.ok) throw new ApiError(res.status, undefined);
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${id}.agenticmind.json`;
    return { blob: await res.blob(), filename };
  },
};

/** Subscribe to live trace events over SSE. Returns an unsubscribe function. */
export function subscribeRunEvents(
  runId: string,
  onEvent: (event: TraceEvent) => void,
  onEnd: () => void,
): () => void {
  const source = new EventSource(`/api/runs/${runId}/events`);
  source.addEventListener("trace", (msg) => onEvent(JSON.parse((msg as MessageEvent).data) as TraceEvent));
  source.addEventListener("end", () => {
    source.close();
    onEnd();
  });
  return () => source.close();
}

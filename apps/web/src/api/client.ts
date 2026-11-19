import type {
  AppPreview,
  AssistantResult,
  AssistantStatus,
  AssistantTurn,
  Deployment,
  NodePackage,
  NodePackages,
  DeployPlan,
  DeployRequest,
  DeployTargets,
  ArtifactInfo,
  ComparisonOut,
  ConversationSummary,
  ConversationTurn,
  DatasetOut,
  Diagnostic,
  EvalCase,
  EvalCaseResult,
  EvaluationConfig,
  EvaluationOut,
  EvaluatorInfo,
  JsonValue,
  MemoryRecord,
  NodeManifest,
  ProjectDocument,
  ProjectOut,
  ProjectSummary,
  RunOut,
  TemplateInfo,
  TraceEvent,
  ValidationResult,
  WaitOut,
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

/** Requests that get no answer within this time fail instead of waiting forever. */
export const REQUEST_TIMEOUT_MS = 15_000;
const UPLOAD_TIMEOUT_MS = 180_000;

/** status 0 = the API could not be reached (down, hung, or the dev proxy could not connect). */
export function isConnectionError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 0;
}

async function send(url: string, init: RequestInit | undefined, timeoutMs: number): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(url, { ...init, signal: init?.signal ?? AbortSignal.timeout(timeoutMs) });
  } catch (err) {
    const timedOut = err instanceof DOMException && (err.name === "TimeoutError" || err.name === "AbortError");
    throw new ApiError(0, {
      message: timedOut
        ? `The API did not respond within ${Math.round(timeoutMs / 1000)} s`
        : "The API is not reachable",
    });
  }
  // The Vite dev proxy answers 502/503/504 (or a plain-text 500) when it cannot reach the API.
  const plainText = !(res.headers.get("content-type") ?? "").includes("application/json");
  if ([502, 503, 504].includes(res.status) || (res.status === 500 && plainText)) {
    throw new ApiError(0, { message: "The API is not reachable" });
  }
  return res;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await send(
    `/api${path}`,
    { ...init, headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) } },
    REQUEST_TIMEOUT_MS,
  );
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

/** Quick liveness probe used to recover automatically once the API is back. */
export async function apiAlive(): Promise<boolean> {
  try {
    const res = await fetch("/api/health", { signal: AbortSignal.timeout(4000) });
    return res.ok;
  } catch {
    return false;
  }
}

/** Designing a graph can take a model a minute or more (several repair rounds). */
export const ASSISTANT_TIMEOUT_MS = 240_000;

export const api = {
  nodes: () => request<NodeManifest[]>("/nodes"),
  assistantStatus: () => request<AssistantStatus>("/assistant"),
  async designWorkflow(
    projectId: string,
    body: { message: string; history: AssistantTurn[]; document?: ProjectDocument },
  ): Promise<AssistantResult> {
    const res = await send(
      `/api/projects/${projectId}/assistant`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      ASSISTANT_TIMEOUT_MS,
    );
    if (!res.ok) {
      let detail: unknown = undefined;
      try {
        detail = (await res.json()).detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as AssistantResult;
  },
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
  resumeRun: (runId: string, body: { responses?: Record<string, JsonValue>; force?: boolean }) =>
    request<RunOut>(`/runs/${runId}/resume`, { method: "POST", ...json(body) }),
  approvals: (projectId: string, status = "pending") =>
    request<WaitOut[]>(`/projects/${projectId}/approvals?status=${encodeURIComponent(status)}`),
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
    const res = await send(`/api/projects/${projectId}/files`, { method: "POST", body }, UPLOAD_TIMEOUT_MS);
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
  nodePackages: () => request<NodePackages>("/plugins"),
  async installNodePackage(file: File, trust: string): Promise<NodePackage> {
    const body = new FormData();
    body.append("file", file, file.name);
    body.append("trust", trust);
    const res = await send(`/api/plugins`, { method: "POST", body }, UPLOAD_TIMEOUT_MS * 4);
    if (!res.ok) {
      let detail: unknown = undefined;
      try {
        detail = (await res.json()).detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, detail);
    }
    return (await res.json()) as NodePackage;
  },
  approveNodePackage: (name: string, permissions: string[], sha256: string) =>
    request<NodePackage>(`/plugins/${encodeURIComponent(name)}/approve`, {
      method: "POST",
      ...json({ permissions, sha256, confirm: true }),
    }),
  setNodePackageEnabled: (name: string, enabled: boolean) =>
    request<NodePackage>(`/plugins/${encodeURIComponent(name)}/${enabled ? "enable" : "disable"}`, { method: "POST" }),
  removeNodePackage: (name: string) => request<void>(`/plugins/${encodeURIComponent(name)}`, { method: "DELETE" }),
  async nodePackageWheel(name: string): Promise<{ blob: Blob; filename: string }> {
    const res = await send(`/api/plugins/${encodeURIComponent(name)}/wheel`, undefined, REQUEST_TIMEOUT_MS * 4);
    if (!res.ok) throw new ApiError(res.status, undefined);
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${name}.whl`;
    return { blob: await res.blob(), filename };
  },
  deleteFile: (projectId: string, fileId: string) =>
    request<void>(`/projects/${projectId}/files/${fileId}`, { method: "DELETE" }),
  memories: (projectId: string) => request<MemoryRecord[]>(`/projects/${projectId}/memories`),
  deleteMemory: (projectId: string, memoryId: string) =>
    request<void>(`/projects/${projectId}/memories/${encodeURIComponent(memoryId)}`, { method: "DELETE" }),
  clearMemories: (projectId: string) =>
    request<{ deleted: number }>(`/projects/${projectId}/memories`, { method: "DELETE" }),
  conversations: (projectId: string) => request<ConversationSummary[]>(`/projects/${projectId}/conversations`),
  conversation: (projectId: string, id: string) =>
    request<ConversationTurn[]>(`/projects/${projectId}/conversations/${encodeURIComponent(id)}`),
  deleteConversation: (projectId: string, id: string) =>
    request<{ deleted: number }>(`/projects/${projectId}/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  fileUrl: (projectId: string, fileId: string) => `/api/projects/${projectId}/files/${fileId}/content`,
  revisions: (id: string) =>
    request<{ revision: number; content_hash: string; created_at: string | null }[]>(`/projects/${id}/revisions`),
  evaluators: () => request<EvaluatorInfo[]>("/evaluators"),
  datasets: (projectId: string) => request<DatasetOut[]>(`/projects/${projectId}/datasets`),
  dataset: (id: string, version?: number) =>
    request<DatasetOut>(`/datasets/${id}${version ? `?version=${version}` : ""}`),
  datasetVersions: (id: string) => request<DatasetOut[]>(`/datasets/${id}/versions`),
  createDataset: (projectId: string, body: { name: string; description?: string; cases: EvalCase[] }) =>
    request<DatasetOut>(`/projects/${projectId}/datasets`, { method: "POST", ...json(body) }),
  saveDataset: (id: string, body: { name: string; description?: string; cases: EvalCase[] }) =>
    request<DatasetOut>(`/datasets/${id}`, { method: "PUT", ...json(body) }),
  deleteDataset: (id: string) => request<void>(`/datasets/${id}`, { method: "DELETE" }),
  startEvaluation: (
    projectId: string,
    body: { dataset_id: string; dataset_version?: number; revision?: number; label: string; config: EvaluationConfig },
  ) => request<EvaluationOut>(`/projects/${projectId}/evaluations`, { method: "POST", ...json(body) }),
  evaluations: (projectId: string) => request<EvaluationOut[]>(`/projects/${projectId}/evaluations`),
  evaluationCases: (id: string) => request<EvalCaseResult[]>(`/evaluations/${id}/cases`),
  compareEvaluations: (a: string, b: string) => request<ComparisonOut>(`/evaluations/compare?a=${a}&b=${b}`),
  setBaseline: (id: string) => request<EvaluationOut>(`/evaluations/${id}/baseline`, { method: "PUT" }),
  cancelEvaluation: (id: string) => request<EvaluationOut>(`/evaluations/${id}/cancel`, { method: "POST" }),
  deleteEvaluation: (id: string) => request<void>(`/evaluations/${id}`, { method: "DELETE" }),
  appPreview: (id: string) => request<AppPreview>(`/projects/${id}/app/preview`),
  deployTargets: () => request<DeployTargets>(`/deployment/targets`),
  deployPlan: (id: string, body: DeployRequest) =>
    request<DeployPlan>(`/projects/${id}/deployments/plan`, { method: "POST", ...json(body) }),
  deploy: (id: string, body: DeployRequest & { plan_hash: string; confirm: boolean; rollback_of?: string | null }) =>
    request<Deployment>(`/projects/${id}/deployments`, { method: "POST", ...json(body) }),
  listDeployments: (id: string) => request<Deployment[]>(`/projects/${id}/deployments`),
  getDeployment: (id: string) => request<Deployment>(`/deployments/${id}`),
  stopDeployment: (id: string) => request<Deployment>(`/deployments/${id}/stop`, { method: "POST" }),
  checkDeploymentHealth: (id: string) => request<Deployment>(`/deployments/${id}/health`, { method: "POST" }),
  registerDeploymentUrl: (id: string, url: string) =>
    request<Deployment>(`/deployments/${id}/url`, { method: "POST", ...json({ url }) }),
  async deploymentBundle(id: string): Promise<{ blob: Blob; filename: string }> {
    const res = await send(`/api/deployments/${id}/bundle`, undefined, REQUEST_TIMEOUT_MS * 4);
    if (!res.ok) throw new ApiError(res.status, undefined);
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${id}.zip`;
    return { blob: await res.blob(), filename };
  },
  async exportApp(id: string): Promise<{ blob: Blob; filename: string }> {
    const res = await send(`/api/projects/${id}/app`, undefined, REQUEST_TIMEOUT_MS * 4);
    if (!res.ok) {
      let detail: unknown = undefined;
      try {
        detail = (await res.json()).detail;
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(res.status, detail);
    }
    const disposition = res.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${id}.zip`;
    return { blob: await res.blob(), filename };
  },
  async exportIR(id: string): Promise<{ blob: Blob; filename: string }> {
    const res = await send(`/api/projects/${id}/export`, undefined, REQUEST_TIMEOUT_MS);
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

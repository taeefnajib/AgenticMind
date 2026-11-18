import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type EdgeChange,
  type IsValidConnection,
  type NodeChange,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, ApiError, apiAlive, isConnectionError, subscribeRunEvents } from "./api/client";
import type {
  ArtifactInfo,
  Diagnostic,
  IRNode,
  JsonValue,
  Mode,
  NodeManifest,
  NodeRunStatus,
  ProjectDocument,
  ProjectOut,
  ProjectSummary,
  RunOut,
  TemplateInfo,
  TraceEvent,
} from "./api/types";
import { AmNode } from "./components/AmNode";
import type { ChatResult } from "./components/ChatPanel";
import { ConsolePanel, isManualInput, type ConsoleTab } from "./components/ConsolePanel";
import { ExportAppDialog } from "./components/ExportAppDialog";
import { Inspector } from "./components/Inspector";
import { NewProjectDialog } from "./components/NewProjectDialog";
import { DRAG_MIME, NodeLibrary } from "./components/NodeLibrary";
import { Toolbar, type RunTargetOption } from "./components/Toolbar";
import {
  checkConnection,
  defaultConfig,
  docToFlow,
  edgeStyle,
  flowToDoc,
  manifestKey,
  parseHandle,
  uniqueEdgeId,
  uniqueNodeId,
  type AmFlowEdge,
  type AmFlowNode,
} from "./lib/graph";
import { useTheme } from "./lib/useTheme";
import { WorkbenchContext } from "./lib/workbench";

const nodeTypes = { am: AmNode };

const STATUS_BY_EVENT: Record<string, NodeRunStatus> = {
  node_started: "running",
  node_retrying: "retrying",
  node_succeeded: "succeeded",
  node_failed: "failed",
  node_skipped: "skipped",
  node_cancelled: "cancelled",
  node_waiting: "waiting",
};

const storage = {
  get(key: string): string | null {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key: string, value: string) {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      /* storage unavailable */
    }
  },
};

type Message = { text: string; tone: "info" | "error" | "success" } | null;

export function App() {
  const flow = useReactFlow<AmFlowNode, AmFlowEdge>();
  const [manifestList, setManifestList] = useState<NodeManifest[]>([]);
  const manifests = useMemo(() => new Map(manifestList.map((m) => [manifestKey(m.type, m.version), m])), [manifestList]);
  const [templates, setTemplates] = useState<TemplateInfo[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProject] = useState<ProjectOut | null>(null);
  const [meta, setMeta] = useState({ name: "", description: "" });
  const [nodes, setNodes, onNodesChange] = useNodesState<AmFlowNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<AmFlowEdge>([]);
  const [dirty, setDirty] = useState(false);
  const [changeTick, setChangeTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [valid, setValid] = useState<boolean | null>(null);
  const [run, setRun] = useState<RunOut | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [nodeStatus, setNodeStatus] = useState<Record<string, NodeRunStatus>>({});
  const [iterations, setIterations] = useState<Record<string, number>>({});
  const [approvalsKey, setApprovalsKey] = useState(0);
  const [approvalCount, setApprovalCount] = useState(0);
  const [exportAppOpen, setExportAppOpen] = useState(false);
  const [runInputs, setRunInputs] = useState<Record<string, JsonValue>>({});
  const [mode, setModeState] = useState<Mode>(() => (storage.get("am.mode") as Mode) || "low_code");
  const [consoleTab, setConsoleTab] = useState<ConsoleTab>("problems");
  const [selected, setSelected] = useState<{ node: string | null; edge: string | null }>({ node: null, edge: null });
  const [message, setMessage] = useState<Message>(null);
  const [newOpen, setNewOpen] = useState(false);
  const unsubscribe = useRef<(() => void) | null>(null);
  const [projectFiles, setProjectFiles] = useState<ArtifactInfo[]>([]);
  // Guards one-time startup work: StrictMode runs effects twice in development, which would
  // otherwise create two starter projects on first launch.
  const initialized = useRef(false);
  const [apiDown, setApiDown] = useState<string | null>(null);
  const [runTarget, setRunTarget] = useState("");
  const theme = useTheme();

  const setMode = (m: Mode) => {
    setModeState(m);
    storage.set("am.mode", m);
  };
  const notify = useCallback((text: string, tone: "info" | "error" | "success" = "info") => setMessage({ text, tone }), []);
  const markDirty = useCallback(() => {
    setDirty(true);
    setChangeTick((t) => t + 1);
  }, []);

  const currentDocument = useCallback((): ProjectDocument | null => {
    if (!project) return null;
    return flowToDoc(project.document, meta, nodes, edges);
  }, [project, meta, nodes, edges]);

  // ---- loading ------------------------------------------------------------------------------
  const resetRunState = () => {
    unsubscribe.current?.();
    setRun(null);
    setEvents([]);
    setNodeStatus({});
    setIterations({});
  };

  const applyProject = useCallback(
    (p: ProjectOut) => {
      const { nodes: n, edges: e } = docToFlow(p.document);
      setProject(p);
      setMeta({ name: p.document.project.name, description: p.document.project.description });
      setNodes(n);
      setEdges(e);
      setDirty(false);
      setSelected({ node: null, edge: null });
      setRunTarget("");
      storage.set("am.project", p.id);
      setChangeTick((t) => t + 1);
      window.setTimeout(() => flow.fitView({ padding: 0.2, duration: 200 }), 50);
    },
    [flow, setNodes, setEdges],
  );

  const refreshProjects = useCallback(async () => {
    const list = await api.projects();
    setProjects(list);
    return list;
  }, []);

  const loadWorkspace = useCallback(async () => {
      try {
        const [nodeList, templateList, list] = await Promise.all([api.nodes(), api.templates(), api.projects()]);
        setApiDown(null);
        setManifestList(nodeList);
        setTemplates(templateList);
        setProjects(list);
        if (list.length === 0) {
          const created = await api.createProject({ name: "Hello pipeline", template: "hello-pipeline" });
          setProjects(await api.projects());
          applyProject(created);
          return;
        }
        const last = storage.get("am.project");
        const target = list.find((p) => p.id === last) ?? list[0]!;
        applyProject(await api.getProject(target.id));
      } catch (err) {
        setApiDown((err as Error).message);
      }
  }, [applyProject]);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;
    void loadWorkspace();
    return () => unsubscribe.current?.();
  }, [loadWorkspace]);

  // While the API is unreachable, probe it every few seconds and reload once it answers again.
  useEffect(() => {
    if (!apiDown) return;
    const timer = window.setInterval(async () => {
      if (await apiAlive()) void loadWorkspace();
    }, 4000);
    return () => window.clearInterval(timer);
  }, [apiDown, loadWorkspace]);

  /** Show the banner for connection failures from any action (save, run, create…). */
  const reportError = useCallback(
    (prefix: string, err: unknown) => {
      if (isConnectionError(err)) setApiDown((err as Error).message);
      else notify(`${prefix}: ${(err as Error).message}`, "error");
    },
    [notify],
  );

  // ---- validation (debounced, on the unsaved canvas state) -----------------------------------
  useEffect(() => {
    if (!project || manifestList.length === 0) return;
    const doc = currentDocument();
    if (!doc) return;
    const timer = window.setTimeout(async () => {
      try {
        const report = await api.validate(project.id, doc);
        setDiagnostics(report.diagnostics);
        setValid(report.valid);
      } catch (err) {
        if (err instanceof ApiError && err.diagnostics.length) setDiagnostics(err.diagnostics);
      }
    }, 400);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [changeTick, project?.id, manifestList.length]);

  // ---- project files ------------------------------------------------------------------------
  const projectId = project?.id ?? null;
  useEffect(() => {
    setProjectFiles([]);
    if (!projectId) return;
    api.files(projectId).then(setProjectFiles).catch(() => setProjectFiles([]));
  }, [projectId]);

  const uploadFiles = useCallback(
    async (list: FileList | File[]) => {
      if (!projectId) return [];
      const added: ArtifactInfo[] = [];
      try {
        for (const file of Array.from(list)) added.push(await api.uploadFile(projectId, file));
        notify(`Uploaded ${added.map((a) => a.filename).join(", ")}`, "success");
      } finally {
        setProjectFiles(await api.files(projectId));
      }
      return added;
    },
    [projectId, notify],
  );

  const removeFile = useCallback(
    async (fileId: string) => {
      if (!projectId) return;
      await api.deleteFile(projectId, fileId);
      setProjectFiles(await api.files(projectId));
      setChangeTick((t) => t + 1);
    },
    [projectId],
  );

  const filesState = useMemo(
    () => ({ projectId, files: projectFiles, upload: uploadFiles, remove: removeFile }),
    [projectId, projectFiles, uploadFiles, removeFile],
  );

  const diagnosticsByNode = useMemo(() => {
    const map = new Map<string, Diagnostic[]>();
    for (const d of diagnostics) {
      for (const id of new Set([d.node_id, ...(d.related_node_ids ?? [])])) {
        if (id) map.set(id, [...(map.get(id) ?? []), d]);
      }
    }
    return map;
  }, [diagnostics]);

  // ---- actions -------------------------------------------------------------------------------
  const save = useCallback(async (): Promise<ProjectOut | null> => {
    const doc = currentDocument();
    if (!project || !doc) return null;
    setBusy(true);
    try {
      const saved = await api.saveProject(project.id, doc, project.revision);
      setProject(saved);
      setDirty(false);
      setProjects(await refreshProjects());
      notify(saved.revision === project.revision ? "No changes to save" : `Saved revision ${saved.revision}`, "success");
      return saved;
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        notify("This project changed elsewhere. Reload it before saving.", "error");
      } else if (err instanceof ApiError && err.diagnostics.length) {
        setDiagnostics(err.diagnostics);
        setConsoleTab("problems");
        notify("The document does not match the IR schema", "error");
      } else reportError("Save failed", err);
      return null;
    } finally {
      setBusy(false);
    }
  }, [currentDocument, project, notify, refreshProjects]);

  const validateNow = useCallback(async () => {
    const doc = currentDocument();
    if (!project || !doc) return;
    const report = await api.validate(project.id, doc);
    setDiagnostics(report.diagnostics);
    setValid(report.valid);
    setConsoleTab("problems");
    notify(report.valid ? "Graph is valid" : `${report.diagnostics.filter((d) => d.severity === "error").length} error(s) found`, report.valid ? "success" : "error");
  }, [currentDocument, project, notify]);

  /**
   * Save if needed, start a run, stream its events onto the canvas and resolve with the finished run.
   * Shared by the Run button and the Chat panel. Resolves null if the run could not start.
   */
  /** Stream a run's events (after ``startSeq``) into the canvas until it ends or starts waiting. */
  const follow = useCallback(
    (runId: string, opts: { switchTabs: boolean }, startSeq: number): Promise<ChatResult> => {
      const collected: TraceEvent[] = [];
      return new Promise<ChatResult>((resolve) => {
        let lastSeq = startSeq;
        unsubscribe.current?.();
        unsubscribe.current = subscribeRunEvents(
          runId,
          (e) => {
            if (e.seq <= lastSeq) return;
            lastSeq = e.seq;
            collected.push(e);
            setEvents((prev) => [...prev, e]);
            const st = STATUS_BY_EVENT[e.event_type];
            if (st && e.node_id) setNodeStatus((prev) => ({ ...prev, [e.node_id!]: st }));
            if (e.event_type === "node_succeeded" && e.node_id && e.data?.iteration) {
              setIterations((prev) => ({ ...prev, [e.node_id!]: (prev[e.node_id!] ?? 0) + 1 }));
            }
          },
          async () => {
            const final = await api.getRun(runId);
            setRun(final);
            setApprovalsKey((k) => k + 1);
            if (final.status === "waiting") {
              setConsoleTab("approvals");
              notify("Run paused: waiting for approval", "info");
            } else {
              if (opts.switchTabs) setConsoleTab("output");
              notify(`Run ${final.status}`, final.status === "succeeded" ? "success" : final.status === "failed" ? "error" : "info");
            }
            resolve({ run: final, events: collected });
          },
        );
      });
    },
    [notify],
  );

  /** Resume a waiting (or interrupted) run and follow it from where its trace left off. */
  const resumeRun = useCallback(
    async (runId: string, body: { responses?: Record<string, JsonValue>; force?: boolean }) => {
      const resumed = await api.resumeRun(runId, body);
      resetRunState();
      const past = await api.events(runId);
      const statuses: Record<string, NodeRunStatus> = {};
      for (const e of past) {
        const st = STATUS_BY_EVENT[e.event_type];
        if (st && e.node_id) statuses[e.node_id] = st;
      }
      setRun(resumed);
      setEvents(past);
      setNodeStatus(statuses);
      notify("Run resumed");
      await follow(runId, { switchTabs: true }, past.reduce((m, e) => Math.max(m, e.seq), 0));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [follow, notify],
  );

  const executeRun = useCallback(
    async (
      inputs: Record<string, JsonValue>,
      targets: string[] | undefined,
      opts: { switchTabs: boolean },
    ): Promise<ChatResult | null> => {
      if (!project) return null;
      if (dirty && !(await save())) return null;
      resetRunState();
      try {
        const r = await api.startRun(project.id, inputs, targets);
        setRun(r);
        if (opts.switchTabs) setConsoleTab("trace");
        notify(`Run ${r.id} started`);
        return await follow(r.id, opts, 0);
      } catch (err) {
        if (err instanceof ApiError && err.status === 422) {
          setDiagnostics(err.diagnostics);
          setValid(false);
          setConsoleTab("problems");
          notify("Fix the problems before running", "error");
        } else reportError("Run failed to start", err);
        return null;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [project, dirty, save, notify, reportError, follow],
  );

  const startRun = useCallback(async () => {
    const targets = runTarget && nodes.some((n) => n.id === runTarget) ? [runTarget] : undefined;
    await executeRun(runInputs, targets, { switchTabs: true });
  }, [executeRun, runInputs, runTarget, nodes]);

  const askChat = useCallback(
    (inputs: Record<string, JsonValue>, target: string) => executeRun(inputs, [target], { switchTabs: false }),
    [executeRun],
  );

  const cancelRun = useCallback(async () => {
    if (!run) return;
    try {
      await api.cancelRun(run.id);
      notify("Cancelling…");
    } catch (err) {
      notify((err as Error).message, "error");
    }
  }, [run, notify]);

  const exportIR = useCallback(async () => {
    if (!project) return;
    if (dirty && !(await save())) return;
    const { blob, filename } = await api.exportIR(project.id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    notify(`Exported ${filename}`, "success");
  }, [project, dirty, save, notify]);

  const ensureSaved = useCallback(async () => !dirty || Boolean(await save()), [dirty, save]);

  const selectProject = useCallback(
    async (id: string) => {
      if (dirty) await save();
      resetRunState();
      setRunInputs({});
      applyProject(await api.getProject(id));
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dirty, save, applyProject],
  );

  const createProject = useCallback(
    async (name: string, template: string | null, document?: unknown) => {
      try {
        if (dirty) await save();
        const created = await api.createProject({ name, template: template ?? undefined, document });
        setNewOpen(false);
        resetRunState();
        setRunInputs({});
        await refreshProjects();
        applyProject(created);
        notify(`Created ${created.name}`, "success");
      } catch (err) {
        reportError("Could not create project", err);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dirty, save, refreshProjects, applyProject, notify],
  );

  const importFile = useCallback(
    async (file: File) => {
      try {
        const doc = JSON.parse(await file.text()) as ProjectDocument;
        await createProject(doc.project?.name || file.name.replace(/\.json$/, ""), null, doc);
      } catch (err) {
        notify(`Import failed: ${(err as Error).message}`, "error");
      }
    },
    [createProject, notify],
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        void save();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save]);

  // ---- canvas editing ------------------------------------------------------------------------
  const handleNodesChange = useCallback(
    (changes: NodeChange<AmFlowNode>[]) => {
      onNodesChange(changes);
      if (changes.some((c) => c.type === "remove" || c.type === "add" || (c.type === "position" && c.dragging === false))) markDirty();
    },
    [onNodesChange, markDirty],
  );

  const handleEdgesChange = useCallback(
    (changes: EdgeChange<AmFlowEdge>[]) => {
      onEdgesChange(changes);
      if (changes.some((c) => c.type === "remove" || c.type === "add")) markDirty();
    },
    [onEdgesChange, markDirty],
  );

  const isValidConnection: IsValidConnection<AmFlowEdge> = useCallback((conn) => {
    const s = parseHandle(conn.sourceHandle);
    const t = parseHandle(conn.targetHandle);
    return Boolean(s && t && s.kind === t.kind && s.dir === "out" && t.dir === "in" && conn.source !== conn.target);
  }, []);

  const onConnect = useCallback(
    (conn: Connection) => {
      const check = checkConnection(conn, nodes, edges, manifests);
      if (!check.ok) {
        notify(check.reason, "error");
        return;
      }
      const branch = check.kind === "control" ? parseHandle(conn.sourceHandle)?.port : undefined;
      const edge: AmFlowEdge = {
        id: uniqueEdgeId(edges.map((e) => e.id)),
        source: conn.source,
        target: conn.target,
        sourceHandle: conn.sourceHandle,
        targetHandle: conn.targetHandle,
        data: { kind: check.kind },
        ...edgeStyle(check.kind, branch),
      };
      setEdges((es) => [...es, edge]);
      markDirty();
    },
    [nodes, edges, manifests, notify, setEdges, markDirty],
  );

  const addNode = useCallback(
    (manifest: NodeManifest, position?: { x: number; y: number }) => {
      const id = uniqueNodeId(manifest.type, nodes.map((n) => n.id));
      const pos =
        position ??
        flow.screenToFlowPosition({ x: window.innerWidth / 2 - 120 + Math.random() * 40, y: window.innerHeight / 2 - 120 + Math.random() * 40 });
      const ir: IRNode = { id, type: manifest.type, version: manifest.version, config: defaultConfig(manifest) };
      setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), { id, type: "am", position: pos, data: { ir }, selected: true }]);
      markDirty();
    },
    [nodes, flow, setNodes, markDirty],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const key = e.dataTransfer.getData(DRAG_MIME);
      const manifest = manifests.get(key);
      if (!manifest) return;
      addNode(manifest, flow.screenToFlowPosition({ x: e.clientX - 120, y: e.clientY - 30 }));
    },
    [manifests, addNode, flow],
  );

  const updateNode = useCallback(
    (id: string, update: (ir: IRNode) => IRNode) => {
      setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ir: update(n.data.ir) } } : n)));
      markDirty();
    },
    [setNodes, markDirty],
  );

  const onSelectionChange = useCallback(({ nodes: ns, edges: es }: OnSelectionChangeParams) => {
    setSelected({ node: ns.length === 1 ? ns[0]!.id : null, edge: ns.length === 0 && es.length === 1 ? es[0]!.id : null });
  }, []);

  const focusNode = useCallback(
    (id: string) => {
      setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === id })));
      setEdges((es) => es.map((e) => ({ ...e, selected: false })));
      setSelected({ node: id, edge: null });
      flow.fitView({ nodes: [{ id }], duration: 300, maxZoom: 1.2, padding: 0.6 });
    },
    [setNodes, setEdges, flow],
  );

  const focusDiagnostic = useCallback(
    (d: Diagnostic) => {
      if (d.node_id && nodes.some((n) => n.id === d.node_id)) focusNode(d.node_id);
      else if (d.edge_id) {
        setEdges((es) => es.map((e) => ({ ...e, selected: e.id === d.edge_id })));
        setNodes((ns) => ns.map((n) => ({ ...n, selected: false })));
        setSelected({ node: null, edge: d.edge_id });
      }
    },
    [nodes, focusNode, setEdges, setNodes],
  );

  const selectedNode = nodes.find((n) => n.id === selected.node) ?? null;
  const selectedEdge = edges.find((e) => e.id === selected.edge) ?? null;
  const inputNodes = nodes.map((n) => n.data.ir).filter(isManualInput);
  const runTargets: RunTargetOption[] = nodes
    .map((n) => n.data.ir)
    .filter((ir) => ir.type === "agenticmind.core.output")
    .map((ir) => ({ id: ir.id, label: ir.ui?.label || String(ir.config.key ?? ir.id) }));
  // Active until the latest run-level event is an ending one (a resumed run has an earlier run_waiting).
  const lastRunEvent = [...events].reverse().find((e) => e.event_type.startsWith("run_"));
  const running =
    run !== null &&
    (run.status === "queued" || run.status === "running") &&
    (!lastRunEvent || lastRunEvent.event_type === "run_started" || lastRunEvent.event_type === "run_resumed");

  const workbench = useMemo(
    () => ({ manifests, nodeStatus, iterations, diagnosticsByNode, mode, files: filesState, dark: theme.resolved === "dark" }),
    [manifests, nodeStatus, iterations, diagnosticsByNode, mode, filesState, theme.resolved],
  );

  return (
    <WorkbenchContext.Provider value={workbench}>
      <div className="flex h-full flex-col">
        <Toolbar
          runTargets={runTargets}
          runTarget={runTarget}
          onRunTarget={setRunTarget}
          projects={projects}
          projectId={project?.id ?? null}
          revision={project?.revision ?? null}
          dirty={dirty}
          busy={busy}
          running={running}
          valid={valid}
          mode={mode}
          message={message}
          onSelectProject={selectProject}
          onNew={() => setNewOpen(true)}
          onSave={() => void save()}
          onValidate={() => void validateNow()}
          onRun={() => void startRun()}
          onCancel={() => void cancelRun()}
          onExport={() => void exportIR()}
          onExportApp={() => setExportAppOpen(true)}
          onMode={setMode}
          theme={theme.preference}
          onTheme={theme.setPreference}
        />
        <div className="flex min-h-0 flex-1">
          <NodeLibrary manifests={manifestList} mode={mode} onAdd={(m) => addNode(m)} />
          <main className="flex min-w-0 flex-1 flex-col">
            <div className="relative min-h-0 flex-1" onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
              {apiDown && <ApiDownBanner reason={apiDown} onRetry={() => void loadWorkspace()} />}
              <ReactFlow<AmFlowNode, AmFlowEdge>
                nodes={nodes}
                edges={edges}
                nodeTypes={nodeTypes}
                onNodesChange={handleNodesChange}
                onEdgesChange={handleEdgesChange}
                onConnect={onConnect}
                isValidConnection={isValidConnection}
                onSelectionChange={onSelectionChange}
                deleteKeyCode={["Backspace", "Delete"]}
                fitView
                minZoom={0.2}
                proOptions={{ hideAttribution: false }}
                colorMode={theme.resolved}
              >
                <Background variant={BackgroundVariant.Dots} gap={18} size={1} color={theme.resolved === "dark" ? "#2c2f38" : "#d3d6dd"} />
                <Controls position="bottom-left" />
                <MiniMap pannable zoomable position="bottom-right" className="!bg-surface" />
              </ReactFlow>
            </div>
            <ConsolePanel
              tab={consoleTab}
              onTab={setConsoleTab}
              mode={mode}
              diagnostics={diagnostics}
              onSelectDiagnostic={focusDiagnostic}
              inputNodes={inputNodes}
              runInputs={runInputs}
              onRunInputs={setRunInputs}
              run={run}
              events={events}
              onSelectNode={focusNode}
              document={() => currentDocument() ?? ({} as ProjectDocument)}
              chatKey={project?.id ?? "none"}
              projectId={project?.id}
              approvalsKey={`${project?.id}:${approvalsKey}`}
              approvalCount={approvalCount}
              onApprovalCount={setApprovalCount}
              onResume={resumeRun}
              chatOutputs={runTargets.map((t) => ({ ...t, key: String(nodes.find((n) => n.id === t.id)?.data.ir.config.key ?? t.id) }))}
              chatDisabled={!project || Boolean(apiDown)}
              onAsk={askChat}
            />
          </main>
          <Inspector
            node={selectedNode}
            edge={selectedEdge}
            meta={meta}
            onMetaChange={(m) => {
              setMeta(m);
              markDirty();
            }}
            onNodeChange={updateNode}
            onDeleteNode={(id) => {
              void flow.deleteElements({ nodes: [{ id }] }).then(markDirty);
            }}
            onDeleteEdge={(id) => {
              void flow.deleteElements({ edges: [{ id }] }).then(markDirty);
            }}
            diagnostics={diagnostics}
            run={run}
            events={events}
            onSelectDiagnostic={focusDiagnostic}
          />
        </div>
      </div>
      <ExportAppDialog
        open={exportAppOpen}
        projectId={project?.id ?? null}
        ensureSaved={ensureSaved}
        onClose={() => setExportAppOpen(false)}
        notify={notify}
      />
      <NewProjectDialog
        open={newOpen}
        templates={templates}
        onCancel={() => setNewOpen(false)}
        onCreate={(name, template) => void createProject(name, template)}
        onImport={(f) => void importFile(f)}
      />
    </WorkbenchContext.Provider>
  );
}

function ApiDownBanner({ reason, onRetry }: { reason: string; onRetry: () => void }) {
  return (
    <div className="absolute inset-x-0 top-4 z-20 flex justify-center px-4" role="alert">
      <div className="flex max-w-xl items-start gap-3 rounded-lg border border-red-200 bg-surface px-4 py-3 shadow-lg">
        <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-red-500" aria-hidden="true" />
        <div className="min-w-0 text-sm">
          <div className="font-medium text-slate-900">Can’t reach the AgenticMind API</div>
          <div className="mt-0.5 text-slate-600">
            {reason}. Make sure <code className="rounded bg-slate-100 px-1 font-mono text-xs">npm run dev:api</code> is
            running; if it is, stop it (Ctrl+C) and start it again. This page reconnects automatically.
          </div>
        </div>
        <button type="button" className="am-btn shrink-0" onClick={onRetry}>
          Retry
        </button>
      </div>
    </div>
  );
}

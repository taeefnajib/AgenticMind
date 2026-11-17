import { createContext, useContext } from "react";

import type { ArtifactInfo, Diagnostic, Mode, NodeManifest, NodeRunStatus } from "../api/types";

export interface FilesState {
  projectId: string | null;
  files: ArtifactInfo[];
  upload: (files: FileList | File[]) => Promise<ArtifactInfo[]>;
  remove: (fileId: string) => Promise<void>;
}

export interface WorkbenchState {
  manifests: Map<string, NodeManifest>;
  nodeStatus: Record<string, NodeRunStatus>;
  diagnosticsByNode: Map<string, Diagnostic[]>;
  mode: Mode;
  files: FilesState;
}

export const WorkbenchContext = createContext<WorkbenchState>({
  manifests: new Map(),
  nodeStatus: {},
  diagnosticsByNode: new Map(),
  mode: "low_code",
  files: { projectId: null, files: [], upload: async () => [], remove: async () => {} },
});

export const useWorkbench = () => useContext(WorkbenchContext);

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
  /** Times each node ran in the current run (>1 inside loops). */
  iterations: Record<string, number>;
  diagnosticsByNode: Map<string, Diagnostic[]>;
  mode: Mode;
  files: FilesState;
  dark: boolean;
}

export const WorkbenchContext = createContext<WorkbenchState>({
  manifests: new Map(),
  nodeStatus: {},
  iterations: {},
  diagnosticsByNode: new Map(),
  mode: "low_code",
  files: { projectId: null, files: [], upload: async () => [], remove: async () => {} },
  dark: false,
});

export const useWorkbench = () => useContext(WorkbenchContext);

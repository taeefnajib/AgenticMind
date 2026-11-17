import { createContext, useContext } from "react";

import type { Diagnostic, Mode, NodeManifest, NodeRunStatus } from "../api/types";

export interface WorkbenchState {
  manifests: Map<string, NodeManifest>;
  nodeStatus: Record<string, NodeRunStatus>;
  diagnosticsByNode: Map<string, Diagnostic[]>;
  mode: Mode;
}

export const WorkbenchContext = createContext<WorkbenchState>({
  manifests: new Map(),
  nodeStatus: {},
  diagnosticsByNode: new Map(),
  mode: "low_code",
});

export const useWorkbench = () => useContext(WorkbenchContext);

import "@xyflow/react/dist/style.css";
import "./index.css";

import { ReactFlowProvider } from "@xyflow/react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { AuthGate } from "./components/AuthGate";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ReactFlowProvider>
      <AuthGate>
        <App />
      </AuthGate>
    </ReactFlowProvider>
  </StrictMode>,
);

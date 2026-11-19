import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiTarget = process.env.AGENTICMIND_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // keep the browser's Host: the API checks that writes come from the page's own origin (ADR-0023)
      "/api": { target: apiTarget, changeOrigin: false },
    },
  },
  test: {
    environment: "node",
  },
} as Parameters<typeof defineConfig>[0]);

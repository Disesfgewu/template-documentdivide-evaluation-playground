import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Dev server proxies /api/* to the LAB API (default http://127.0.0.1:8000).
const apiTarget = process.env.LAB_API_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": { target: apiTarget, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  test: { environment: "jsdom" },
});

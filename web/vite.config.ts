import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Ports come from scripts/dev.sh (BACKEND_PORT / FRONTEND_PORT); defaults match it.
const backend = process.env.BACKEND_PORT ?? "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.FRONTEND_PORT ?? 5173),
    strictPort: true,
    proxy: {
      "/api/ws": { target: `ws://localhost:${backend}`, ws: true },
      "/api": `http://localhost:${backend}`,
    },
  },
});

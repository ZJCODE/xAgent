import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  base: "/static/",
  plugins: [react()],
  build: {
    outDir: "../xagent/interfaces/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8010",
      "/chat": "http://127.0.0.1:8010",
      "/observe": "http://127.0.0.1:8010",
      "/clear_messages": "http://127.0.0.1:8010",
      "/health": "http://127.0.0.1:8010",
      "/ws": {
        target: "ws://127.0.0.1:8010",
        ws: true,
      },
    },
  },
});

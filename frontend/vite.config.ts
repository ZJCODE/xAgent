import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const WEB_CLIENT_ORIGIN = "http://127.0.0.1:1415";

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
      "/api": WEB_CLIENT_ORIGIN,
      "/chat": WEB_CLIENT_ORIGIN,
      "/observe": WEB_CLIENT_ORIGIN,
      "/clear_messages": WEB_CLIENT_ORIGIN,
      "/health": WEB_CLIENT_ORIGIN,
      "/ws": {
        target: "ws://127.0.0.1:1415",
        ws: true,
      },
    },
  },
});

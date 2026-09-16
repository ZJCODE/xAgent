import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(root, "../agents_world/static");

function emitIndexHtml() {
  return {
    name: "emit-index-html",
    closeBundle() {
      const from = path.join(outDir, "world.html");
      const to = path.join(outDir, "index.html");
      if (fs.existsSync(from)) fs.renameSync(from, to);
    },
  };
}

export default defineConfig({
  base: "/",
  plugins: [react(), emitIndexHtml()],
  build: {
    outDir,
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: path.resolve(root, "world.html"),
    },
  },
  server: {
    port: 5174,
    proxy: {
      "/neighbors": "http://127.0.0.1:7182",
      "/worlds": "http://127.0.0.1:7182",
      "/files": "http://127.0.0.1:7182",
    },
  },
});

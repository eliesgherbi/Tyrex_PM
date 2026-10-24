import { defineConfig } from "vite";

export default defineConfig({
  server: { host: "127.0.0.1", port: 4175, strictPort: true },
  build: { outDir: "dist-workbench", emptyOutDir: true, rollupOptions: { input: "workbench.html" } },
});

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The build ships inside the Python package, so `devflow ui` needs no Node at run time.
// `npm run dev` proxies the API to a running `devflow ui --port 8765` (open it once with its token link first).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  build: {
    outDir: "../src/dev_workflows/ui/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: { proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } } },
});

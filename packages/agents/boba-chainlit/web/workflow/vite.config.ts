import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// base "./": index.html отдаёт сервер с <base href> под url_prefix приложения
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../../assets/public/workflow",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          flow: ["@xyflow/react", "@dagrejs/dagre"],
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** Сборка относительная: index.html отдаёт chainlit с <base href> под своим
 * префиксом. Отдельного dev-сервера у страницы нет: правки проверяются
 * пересборкой (make web-catalog) и копией dist в app_root/public/catalog. */
export default defineConfig(() => ({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../../assets/catalog",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          flow: ["@xyflow/react"],
          elk: ["elkjs/lib/elk.bundled.js"],
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
}));

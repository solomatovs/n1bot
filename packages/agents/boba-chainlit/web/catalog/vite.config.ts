import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** Dev-сервер живёт под {BOBA_URL_PREFIX}/catalog-dev/ — туда его проксирует
 * приложение; сборка относительная: index.html отдаёт сервер с <base href>. */
function devBase(): string {
  const prefix = process.env["BOBA_URL_PREFIX"];
  if (prefix === undefined) {
    throw new Error("BOBA_URL_PREFIX is required to run the dev server");
  }

  return `${prefix}/catalog-dev/`;
}

export default defineConfig(({ command, mode }) => ({
  plugins: [react()],
  base: command === "serve" && mode !== "test" ? devBase() : "./",
  server: {
    host: true,
    port: 5174,
    strictPort: true,
  },
  build: {
    outDir: "../../assets/catalog",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          flow: ["@xyflow/react"],
          elk: ["elkjs"],
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
}));

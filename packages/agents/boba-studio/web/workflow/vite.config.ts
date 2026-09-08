import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/** Dev-сервер живёт под {BOBA_URL_PREFIX}/workflow-dev/ — туда его проксирует
 * приложение; сборка относительная: index.html отдаёт сервер с <base href>. */
function devBase(): string {
  const prefix = process.env["BOBA_URL_PREFIX"];
  if (prefix === undefined) {
    throw new Error("BOBA_URL_PREFIX is required to run the dev server");
  }

  return `${prefix}/workflow-dev/`;
}

/** Вендоры по чанкам; код каталога отделяется сам по ленивому импорту
 * секции (catalog/services.tsx), ELK нужен только ему. */
function chunkOf(id: string): string | undefined {
  if (id.includes("node_modules/elkjs/")) {
    return "elk";
  }

  if (/node_modules\/(react|react-dom|react-router|react-router-dom|scheduler)\//.test(id)) {
    return "react";
  }

  if (id.includes("node_modules/@xyflow/") || id.includes("node_modules/@dagrejs/")) {
    return "flow";
  }

  return undefined;
}

export default defineConfig(({ command, mode }) => ({
  plugins: [react()],
  base: command === "serve" && mode !== "test" ? devBase() : "./",
  server: {
    host: true,
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "../../assets/workflow",
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: chunkOf,
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
}));

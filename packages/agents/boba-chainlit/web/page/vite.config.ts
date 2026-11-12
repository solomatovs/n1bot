import { defineConfig } from "vite";

/** Скрипт страницы chainlit одним IIFE-файлом: сервер подставляет адреса в плейсхолдеры
 * и отдаёт его через custom_js; для F5 сборка кладётся в assets/public пакета. */
export default defineConfig({
  build: {
    outDir: "../../assets/public",
    emptyOutDir: false,
    sourcemap: false,
    minify: false,
    lib: {
      entry: "src/page.ts",
      formats: ["iife"],
      name: "bobaPage",
      fileName: () => "page.js",
    },
  },
});

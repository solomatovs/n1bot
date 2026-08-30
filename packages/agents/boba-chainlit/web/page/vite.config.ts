import { defineConfig } from "vite";

/** Скрипт страницы chainlit одним IIFE-файлом: сервер подставляет адреса в плейсхолдеры
 * и отдаёт его через custom_js; сборка кладётся в assets/public рядом с остальной статикой. */
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

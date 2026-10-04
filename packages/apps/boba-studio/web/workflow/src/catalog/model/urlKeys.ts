/** Ключи строки запроса страниц каталога: их читают urlState и пишут ссылки
 * из PageUrls; модуль без зависимостей, чтобы адреса жили в стартовом чанке. */

/** Вкладка левой панели: процесс (черновики и узлы по слоям) или деревья снимков подключений. */
export type PaneTab = "process" | "connections";

/** Ключи страницы процесса. */
export const UrlKey = {
  active: "active",
  object: "object",
  showMode: "mode",
  hidden: "hidden",
  showDiff: "diff",
  pane: "pane",
} as const;

/** Ключи страницы подключения. */
export const ConnectionUrlKey = {
  version: "v",
  mode: "mode",
  ref: "ref",
} as const;

export const DIFF_MODE = "diff";

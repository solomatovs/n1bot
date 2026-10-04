import { useState } from "react";
import { Button } from "@/components/ui/button";
import { PanelRight } from "lucide-react";

// Браузерные API держим в одной точке: при серверном рендере их нет, а
// разбросанные по обработчикам обращения проверить негде.
const browser = {
  get doc() {
    if (typeof document === "undefined") return null;
    return document;
  },
  get win() {
    if (typeof window === "undefined") return null;
    return window;
  },
  find(selector) {
    const doc = this.doc;
    if (!doc) return null;
    return doc.querySelector(selector);
  },
  byId(id) {
    const doc = this.doc;
    if (!doc) return null;
    return doc.getElementById(id);
  },
  create(tag) {
    const doc = this.doc;
    if (!doc) return null;
    return doc.createElement(tag);
  },
  root() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.documentElement;
  },
  head() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.head;
  },
  body() {
    const doc = this.doc;
    if (!doc) return null;
    return doc.body;
  },
  global(name) {
    const win = this.win;
    if (!win) return undefined;
    return win[name];
  },
  emit(event) {
    const win = this.win;
    if (!win) return;
    win.dispatchEvent(event);
  },
  listen(name, handler) {
    const win = this.win;
    if (!win) return;
    win.addEventListener(name, handler);
  },
  unlisten(name, handler) {
    const win = this.win;
    if (!win) return;
    win.removeEventListener(name, handler);
  },
};

// Ссылка на файл в переписке: клик открывает панель справа. Живёт в истории
// треда, поэтому старый чат открывается со всеми своими диаграммами.
export default function CanvasLink() {
  const [busy, setBusy] = useState(false);

  const open = async () => {
    // открытая панель меняет содержимое сама: новый элемент с сервера
    // пересоздал бы её и снова проиграл анимацию открытия
    if (browser.byId("side-view-content")) {
      browser.emit(
        new CustomEvent("boba:canvas", { detail: { path: props.path } })
      );
      return;
    }

    setBusy(true);
    try {
      await callAction({ name: "canvas_open", payload: { path: props.path } });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      variant="outline"
      className="w-fit max-w-full gap-2"
      onClick={open}
      disabled={busy}
      title={props.path}
    >
      <PanelRight />
      <span className="truncate">{props.label || props.path}</span>
    </Button>
  );
}

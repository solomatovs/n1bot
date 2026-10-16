import { useEffect, useRef } from "react";

// Иконка scroll-text из lucide: раннер кастом-элементов не отдаёт react-dom,
// портала нет — кнопка вставляется в заголовок шага готовым DOM-узлом.
const ICON =
  '<svg width="16" height="16" ' +
  'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M15 12h-5"/><path d="M15 8h-5"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/>' +
  '<path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3"/>' +
  "</svg>";

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

// Малозаметная кнопка живого вывода на строке заголовка шага инструмента:
// клик открывает поток в панели, не сворачивая шаг. Контент шага при
// сворачивании размонтируется — кнопка живёт, пока шаг раскрыт.
export default function CanvasStream() {
  const hostRef = useRef(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const content = host.closest("[data-state]");
    const item = content && content.parentElement;
    const trigger = item && item.querySelector('[id^="step-"]');
    if (!trigger) return;

    const button = browser.create("span");
    if (!button) return;
    button.className =
      "inline-flex items-center justify-center h-6 w-6 rounded-md " +
      "opacity-50 hover:opacity-100 hover:bg-accent cursor-pointer";
    button.title = `Live output: ${props.label || props.call_id}`;
    button.setAttribute("role", "button");
    button.setAttribute("aria-label", "Show tool output");
    button.innerHTML = ICON;

    const open = (event) => {
      event.preventDefault();
      event.stopPropagation();
      callAction({ name: "canvas_stream", payload: { call_id: props.call_id } });
    };
    const swallow = (event) => event.stopPropagation();
    button.addEventListener("click", open);
    button.addEventListener("pointerdown", swallow);
    trigger.appendChild(button);

    return () => {
      button.removeEventListener("click", open);
      button.removeEventListener("pointerdown", swallow);
      button.remove();
    };
  }, []);

  return <span ref={hostRef} className="hidden" />;
}

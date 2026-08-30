// Скрипт страницы chainlit: кнопка SSO (если сервер отдал её адрес), placeholder
// пароля и молчаливое обновление входа по сигналу сервера; отказ обновления значит,
// что сессию не продлить, — страница уходит на логин. Адреса и заголовок
// подставляет сервер в плейсхолдеры.

const SSO_URL = "__SSO_URL__";
const REFRESH_URL = "__REFRESH_URL__";
const LOGIN_URL = "__LOGIN_URL__";
const TRANSLATIONS_URL = "__TRANSLATIONS_URL__";
const REFRESH_HEADER = "__REFRESH_HEADER__";
const REFRESH_HEADER_VALUE = "__REFRESH_HEADER_VALUE__";
const REFRESH_SIGNAL = "boba:signin-refresh";
const BTN_ID = "sso-login-btn";
const BTN_CLASS = [
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md",
  "text-sm font-medium ring-offset-background transition-colors",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  "focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
  "bg-primary text-primary-foreground hover:bg-primary/90 h-10 px-4 py-2 w-full",
].join(" ");
const PASSWORD_ID = "password";
const PASSWORD_PLACEHOLDER_PATH = ["translation", "auth", "login", "form", "password", "placeholder"];

type Signal = { type?: unknown };

/** Браузерные API в одной точке: в серверном рендере их не существует. */
const browser = {
  get doc(): Document | null {
    if (typeof document === "undefined") return null;
    return document;
  },
  get win(): Window | null {
    if (typeof window === "undefined") return null;
    return window;
  },
  byId(id: string): HTMLElement | null {
    const doc = this.doc;
    if (doc === null) return null;
    return doc.getElementById(id);
  },
  find(selector: string): Element | null {
    const doc = this.doc;
    if (doc === null) return null;
    return doc.querySelector(selector);
  },
  root(): HTMLElement | null {
    const doc = this.doc;
    if (doc === null) return null;
    return doc.documentElement;
  },
  onReady(handler: () => void): void {
    const doc = this.doc;
    if (doc === null) return;
    doc.addEventListener("DOMContentLoaded", handler);
  },
  path(): string {
    const win = this.win;
    if (win === null) return "";
    return win.location.pathname;
  },
  go(url: string): void {
    const win = this.win;
    if (win === null) return;
    win.location.href = url;
  },
};

/** Строка по пути в объекте переводов; пусто — пути нет или там не строка. */
function pick(value: unknown, path: readonly string[]): string {
  let current: unknown = value;
  for (const key of path) {
    if (current === null || typeof current !== "object") return "";
    current = (current as Record<string, unknown>)[key];
  }
  if (typeof current !== "string") return "";
  return current;
}

// фронт chainlit ставит placeholder только полю логина: у пароля берём его из переводов сами
let passwordPlaceholder = "";
let placeholderRequested = false;

function injectPlaceholder(): void {
  if (passwordPlaceholder === "") return;
  const field = browser.byId(PASSWORD_ID);
  if (!(field instanceof HTMLInputElement)) return;
  if (field.placeholder === passwordPlaceholder) return;
  field.placeholder = passwordPlaceholder;
}

function loadPlaceholder(): void {
  if (placeholderRequested) return;
  placeholderRequested = true;

  const win = browser.win;
  if (win === null) return;
  const language = win.navigator.language || "en-US";
  const url = `${TRANSLATIONS_URL}?language=${encodeURIComponent(language)}`;
  void fetch(url, { credentials: "same-origin" })
    .then((response) => {
      if (!response.ok) return null;
      return response.json() as Promise<unknown>;
    })
    .then((body) => {
      if (body === null) return;
      passwordPlaceholder = pick(body, PASSWORD_PLACEHOLDER_PATH);
      injectPlaceholder();
    })
    .catch(() => undefined);
}

const onLogin = (): boolean => /\/login\/?$/.test(browser.path());

function build(doc: Document): HTMLButtonElement {
  // классы кнопки chainlit (shadcn Button default): без формы пароля клонировать нечего
  const btn = doc.createElement("button");
  btn.id = BTN_ID;
  btn.type = "button";
  btn.className = BTN_CLASS;
  btn.textContent = "Войти через SSO";
  btn.addEventListener("click", (event) => {
    event.preventDefault();
    // навигация, а не fetch: Negotiate браузер делает при переходе по адресу
    browser.go(SSO_URL);
  });
  return btn;
}

function inject(): void {
  if (!onLogin()) {
    const stale = browser.byId(BTN_ID);
    if (stale !== null) stale.remove();
    return;
  }

  loadPlaceholder();
  injectPlaceholder();

  if (SSO_URL.length === 0) return;
  if (browser.byId(BTN_ID) !== null) return;

  const doc = browser.doc;
  if (doc === null) return;
  const form = browser.find("form");
  if (!(form instanceof HTMLFormElement)) return;

  // под кнопкой «Войти», а без формы пароля (только kerberos) — в конец формы
  const submit = form.querySelector('button[type="submit"]');
  if (submit !== null) {
    submit.insertAdjacentElement("afterend", build(doc));
    return;
  }
  form.appendChild(build(doc));
}

// сервер просит обновить вход: обмен идёт без участия пользователя; отказ — на логин
let refreshing = false;

function refresh(): void {
  if (refreshing) return;
  refreshing = true;
  const headers: Record<string, string> = {};
  // заголовок метит запрос как свой: кросс-сайтовый запрос его не поставит
  headers[REFRESH_HEADER] = REFRESH_HEADER_VALUE;
  void fetch(REFRESH_URL, { method: "POST", credentials: "include", headers })
    .then((response) => {
      if (response.ok) return;
      browser.go(LOGIN_URL);
    })
    .catch(() => undefined)
    .then(() => {
      refreshing = false;
    });
}

function onSignal(event: MessageEvent): void {
  // сигнал приходит только от своей страницы: чужое окно обмен не запускает
  const win = browser.win;
  if (win === null) return;
  if (event.source !== win) return;
  if (event.origin !== win.location.origin) return;
  const data: unknown = event.data;
  if (data === null || typeof data !== "object") return;
  if ((data as Signal).type !== REFRESH_SIGNAL) return;
  refresh();
}

const win = browser.win;
if (win !== null) win.addEventListener("message", onSignal);

const root = browser.root();
if (root !== null) {
  const observer = new MutationObserver(() => {
    inject();
  });
  observer.observe(root, { childList: true, subtree: true });
}
browser.onReady(inject);
inject();

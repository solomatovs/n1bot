// Кнопка «Войти через SSO», placeholder пароля и молчаливое обновление тикета;
// __SSO_URL__, __REFRESH_URL__ и __TRANSLATIONS_URL__ подставляет сервер.
(() => {
  "use strict";
  const SSO_URL = "__SSO_URL__";
  const REFRESH_URL = "__REFRESH_URL__";
  const TRANSLATIONS_URL = "__TRANSLATIONS_URL__";
  const REFRESH_SIGNAL = "boba:kerberos-refresh";
  const REFRESH_HEADER = "__REFRESH_HEADER__";
  const REFRESH_HEADER_VALUE = "__REFRESH_HEADER_VALUE__";
  const BTN_ID = "sso-login-btn";
  const BTN_CLASS = [
    "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md",
    "text-sm font-medium ring-offset-background transition-colors",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
    "focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
    "bg-primary text-primary-foreground hover:bg-primary/90 h-10 px-4 py-2 w-full",
  ].join(" ");
  const PASSWORD_ID = "password";
  const PASSWORD_PLACEHOLDER_PATH = ["auth", "login", "form", "password", "placeholder"];

  // фронт chainlit ставит placeholder только полю логина: у пароля берём его из переводов сами
  let passwordPlaceholder = "";
  let placeholderRequested = false;

  function pick(obj, path) {
    let cur = obj;
    for (const key of path) {
      if (cur === null || typeof cur !== "object") return "";
      cur = cur[key];
    }
    if (typeof cur !== "string") return "";
    return cur;
  }

  function loadPlaceholder() {
    if (placeholderRequested) return;
    placeholderRequested = true;

    const win = browser.win;
    if (!win) return;
    const language = win.navigator.language || "en-US";
    const url = TRANSLATIONS_URL + "?language=" + encodeURIComponent(language);
    fetch(url, { credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) return null;
        return r.json();
      })
      .then((body) => {
        if (!body) return;
        passwordPlaceholder = pick(body, ["translation", ...PASSWORD_PLACEHOLDER_PATH]);
        injectPlaceholder();
      })
      .catch(() => {});
  }

  function injectPlaceholder() {
    if (!passwordPlaceholder) return;
    const field = browser.byId(PASSWORD_ID);
    if (!field) return;
    if (field.placeholder === passwordPlaceholder) return;
    field.placeholder = passwordPlaceholder;
  }

  // Браузерные API держим в одной точке: в серверном рендере их не существует.
  const browser = {
    get doc() {
      if (typeof document === "undefined") return null;
      return document;
    },
    get win() {
      if (typeof window === "undefined") return null;
      return window;
    },
    byId(id) {
      const doc = this.doc;
      if (!doc) return null;
      return doc.getElementById(id);
    },
    find(selector) {
      const doc = this.doc;
      if (!doc) return null;
      return doc.querySelector(selector);
    },
    root() {
      const doc = this.doc;
      if (!doc) return null;
      return doc.documentElement;
    },
    onReady(handler) {
      const doc = this.doc;
      if (!doc) return;
      doc.addEventListener("DOMContentLoaded", handler);
    },
    path() {
      const win = this.win;
      if (!win) return "";
      return win.location.pathname;
    },
    go(url) {
      const win = this.win;
      if (!win) return;
      win.location.href = url;
    },
  };


  const onLogin = () => /\/login\/?$/.test(browser.path());

  function build() {
    // классы кнопки chainlit (shadcn Button default): без формы пароля клонировать нечего
    const btn = browser.doc.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.className = BTN_CLASS;
    btn.textContent = "Войти через SSO";
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      // навигация, а не fetch: Negotiate браузер делает при переходе по адресу
      browser.go(SSO_URL);
    });
    return btn;
  }

  function inject() {
    if (!onLogin()) {
      const stale = browser.byId(BTN_ID);
      if (stale) stale.remove();
      return;
    }

    loadPlaceholder();
    injectPlaceholder();

    if (browser.byId(BTN_ID)) return;

    const form = browser.find("form");
    if (!form) return;

    // под кнопкой «Войти», а без формы пароля (только kerberos) — в конец формы
    const submit = form.querySelector('button[type="submit"]');
    if (submit) {
      submit.insertAdjacentElement("afterend", build());
      return;
    }
    form.appendChild(build());
  }

  // сервер просит обменяться заново: браузер домена делает это без участия пользователя
  let refreshing = false;

  function refresh() {
    if (refreshing) return;
    refreshing = true;
    const headers = {};
    headers[REFRESH_HEADER] = REFRESH_HEADER_VALUE;
    // заголовок метит запрос как свой: кросс-сайтовый запрос его не поставит
    fetch(REFRESH_URL, { credentials: "include", headers: headers })
      .catch(() => {})
      .then(() => {
        refreshing = false;
      });
  }

  function onSignal(event) {
    // сигнал приходит только от своей страницы: чужое окно обмен не запускает
    if (event.source !== browser.win) return;
    if (event.origin !== browser.win.location.origin) return;
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type !== REFRESH_SIGNAL) return;
    refresh();
  }

  const win = browser.win;
  if (win) win.addEventListener("message", onSignal);

  const obs = new MutationObserver(() => inject());
  const root = browser.root();
  if (root) obs.observe(root, { childList: true, subtree: true });
  browser.onReady(inject);
  inject();
})();

// Кнопка «Войти через SSO» на странице логина; __SSO_URL__ подставляет сервер.
(() => {
  "use strict";
  const SSO_URL = "__SSO_URL__";
  const BTN_ID = "sso-login-btn";

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

  function build(sample) {
    // клон нативной кнопки: классы, вёрстка и тема наследуются автоматически
    const btn = sample.cloneNode(true);
    btn.id = BTN_ID;
    btn.type = "button";
    btn.textContent = "Войти через SSO";
    btn.removeAttribute("disabled");
    btn.removeAttribute("form");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      fetch(SSO_URL, { credentials: "same-origin" })
        .then((r) => {
          if (r.ok) {
            browser.go(r.url);
          } else {
            browser.go(browser.path() + "?error=sso");
          }
        })
        .catch(() => {
          browser.go(browser.path() + "?error=sso");
        });
    });
    return btn;
  }

  function inject() {
    if (!onLogin()) {
      const stale = browser.byId(BTN_ID);
      if (stale) stale.remove();
      return;
    }
    if (browser.byId(BTN_ID)) return;

    const form = browser.find("form");
    if (!form) return;

    // образец стиля — нативная submit-кнопка формы
    const sample =
      form.querySelector('button[type="submit"]') || form.querySelector("button");
    if (!sample) return;

    sample.insertAdjacentElement("afterend", build(sample));
  }

  const obs = new MutationObserver(() => inject());
  const root = browser.root();
  if (root) obs.observe(root, { childList: true, subtree: true });
  browser.onReady(inject);
  inject();
})();

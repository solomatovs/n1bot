// Кнопка «Войти через SSO» на странице логина; __SSO_URL__ подставляет сервер.
(() => {
  "use strict";
  const SSO_URL = "__SSO_URL__";
  const BTN_ID = "sso-login-btn";

  const onLogin = () => /\/login\/?$/.test(window.location.pathname);

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
            window.location.href = r.url;
          } else {
            window.location.href = window.location.pathname + "?error=sso";
          }
        })
        .catch(() => {
          window.location.href = window.location.pathname + "?error=sso";
        });
    });
    return btn;
  }

  function inject() {
    if (!onLogin()) {
      const stale = document.getElementById(BTN_ID);
      if (stale) stale.remove();
      return;
    }
    if (document.getElementById(BTN_ID)) return;

    const form = document.querySelector("form");
    if (!form) return;

    // образец стиля — нативная submit-кнопка формы
    const sample =
      form.querySelector('button[type="submit"]') || form.querySelector("button");
    if (!sample) return;

    sample.insertAdjacentElement("afterend", build(sample));
  }

  const obs = new MutationObserver(() => inject());
  obs.observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", inject);
  inject();
})();

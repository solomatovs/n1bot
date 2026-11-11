
(() => {
    const publicUrl = new URL(".", document.currentScript.src);

    const lightIcon = new URL("favicon.svg", publicUrl).href;
    const darkIcon = new URL("favicon-dark.svg", publicUrl).href;

    const theme = window.matchMedia(
        "(prefers-color-scheme: dark)"
    );

    function updateFavicon() {
        // Удаляем штатные favicon Chainlit
        document.querySelectorAll('link[rel~="icon"]')
            .forEach(link => link.remove());

        const link = document.createElement("link");
        link.rel = "icon";
        link.type = "image/svg+xml";
        link.href = theme.matches ? darkIcon : lightIcon;

        document.head.appendChild(link);
    }

    updateFavicon();

    // Реагируем на переключение темы браузера/ОС
    theme.addEventListener("change", updateFavicon);
})();


/* AID — системная тема по умолчанию */

(() => {
    const STORAGE_KEY = "vite-ui-theme";

    // При первом посещении выбираем системную тему.
    if (localStorage.getItem(STORAGE_KEY) === null) {
        localStorage.setItem(STORAGE_KEY, "system");

        // Chainlit уже мог инициализировать ThemeProvider.
        // Перезагружаем страницу один раз, чтобы он
        // прочитал новое значение.
        window.location.reload();
        return;
    }

    // Отслеживаем изменение темы операционной системы.
    const systemTheme = window.matchMedia(
        "(prefers-color-scheme: dark)"
    );

    systemTheme.addEventListener("change", (event) => {
        // Не переопределяем ручной выбор пользователя.
        if (localStorage.getItem(STORAGE_KEY) !== "system") {
            return;
        }

        document.documentElement.classList.toggle(
            "dark",
            event.matches
        );

        document.documentElement.classList.toggle(
            "light",
            !event.matches
        );
    });
})();


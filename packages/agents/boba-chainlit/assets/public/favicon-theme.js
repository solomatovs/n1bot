
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

"""boba-tool-web — плагин с web-tools: web_fetch + web_download.

Entry-point модуль для `AgentBuilder.discover_plugins("boba.plugins")`.

Конфиг (всё под одним префиксом `[tool.web.*]`):
- `[tool.web]`           — `enable`/`tools` (framework) + shared
                           `WebConnection` поля (`timeout_sec`, `ssl_verify`).
- `[tool.web.hosts."<host>".auth]` — per-host whitelist+auth-профиль.
                           Hostname не в этом словаре → запрос запрещён.
- `[tool.web.fetch]`     — `WebFetchConfig` (обычно пуст; всё через
                           `defaults_from=("tool.web",)`).
- `[tool.web.download]`  — `WebDownloadConfig` (`dest_dir` обязателен,
                           остальное через `defaults_from`).

`ProjectWorkspaceShell` инжектится через `FromDI(Scope.APP)` — приложение
обязано зарегистрировать provider'а в `AgentBuilder`.
"""

from __future__ import annotations

from boba.tool.web.config import WebPluginConfig
from boba.tool.web.connection import WebConnection
from boba.tool.web.host_profile import WebHostProfile
from boba.tool.web.tools import (
    WebDownloadConfig,
    WebFetchConfig,
    web_download,
    web_fetch,
)

__all__ = [
    "WebConnection",
    "WebDownloadConfig",
    "WebFetchConfig",
    "WebHostProfile",
    "WebPluginConfig",
    "web_download",
    "web_fetch",
]

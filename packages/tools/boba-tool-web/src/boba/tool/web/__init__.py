"""boba-tool-web — плагин с web-tools: web_fetch + web_download.

Entry-point модуль для `AgentBuilder.discover_plugins("boba.plugins")`.

Конфиг:
- `[tool.web]`           — meta-секция плагина (`enable`/`tools` allowlist).
- `[web]`                — shared `WebConnection` (hosts/whitelist, auth,
                           timeout, ssl_verify, cache_dir). Tool-конфиги
                           берут поля отсюда через `defaults_from=("web",)`.
- `[tool.web.fetch]`     — `WebFetchConfig` (обычно пуст, всё из `[web]`).
- `[tool.web.download]`  — `WebDownloadConfig` (`dest_dir` обязателен,
                           остальное из `[web]`).

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

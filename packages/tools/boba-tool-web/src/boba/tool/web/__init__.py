"""boba-tool-web — плагин с web-tools: web_fetch + web_download.

Entry-point модуль для `AgentBuilder.discover_plugins("boba.plugins")`.

Конфиг (всё в секции плагина `[tool.web]`):
- `[tool.web]`           — `enable`/`tools` (framework) + `connection`
                           (`WebConnection`) + `dest_dir` (download).
- `[tool.web.connection].profiles` — `{ "<hostname>" = "${web.<name>}" }`:
                           whitelist хостов → web-профиль (`HttpConnection`:
                           timeout/ssl/auth). Hostname не в dict → запрещён.

`ProjectWorkspaceShell` инжектится через `FromDI(Scope.APP)` — приложение
обязано зарегистрировать provider'а в `AgentBuilder`.
"""

from __future__ import annotations

from boba.tool.web.config import WebPluginConfig
from boba.tool.web.connection import WebConnection
from boba.tool.web.tools import (
    WebDownloadConfig,
    WebGrepConfig,
    web_download,
    web_fetch,
    web_grep,
)

__all__ = [
    "WebConnection",
    "WebDownloadConfig",
    "WebGrepConfig",
    "WebPluginConfig",
    "web_download",
    "web_fetch",
    "web_grep",
]

"""Конфиг плагина html: `[tool.html]` / `BOBA_TOOL__HTML__*`.

Плагин-уровневое включение/allowlist (`enable`, `tools`) — забота
framework'а (`AgentBuilder.discover_plugins`), плагин про них не знает.
`extra="ignore"` позволяет им жить в той же TOML-секции.
"""

from __future__ import annotations

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["HtmlPluginConfig"]


class HtmlPluginConfig(BobaFlatSettings):
    """HTML multi-tool plugin: outline + section.

    Работает с workspace (через `ProjectWorkspaceShell`), без сетевого
    connection. На данный момент plugin-specific полей нет — конфиг
    нужен в первую очередь как FromConfig-якорь.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.html",
    )

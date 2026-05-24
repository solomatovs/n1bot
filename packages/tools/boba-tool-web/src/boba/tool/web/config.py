"""WebPluginConfig: плагин-уровневая meta-секция `[tool.web]`.

Поля `enable` / `tools` читаются framework'ом (`AgentBuilder.discover_plugins`),
плагин про них не знает — `extra='ignore'`. Здесь схема пустая, нужна
только чтобы декларация плагина существовала и framework мог его включить.
"""

from __future__ import annotations

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["WebPluginConfig"]


class WebPluginConfig(BobaFlatSettings):
    """Web-tools plugin config (`[tool.web]`)."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.web",
    )

"""WebPluginConfig: плагин-уровневая meta-секция `[tool.web]`.

Поля `enable` / `tools` читаются framework'ом (`AgentBuilder.discover_plugins`),
плагин про них не знает — `extra='ignore'`. Здесь схема пустая, нужна
только чтобы декларация плагина существовала и framework мог его включить.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["WebPluginConfig"]


class WebPluginConfig(BaseModel):
    """Web-tools plugin config (`[tool.web]`)."""

    model_config = ConfigDict(extra="ignore")

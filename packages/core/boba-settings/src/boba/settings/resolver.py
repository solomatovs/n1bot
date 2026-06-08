"""OmegaConf-backed реализации портов boba.tools (резолв + plugin-фильтр).

Конфиг-инстанс приходит в конструктор (из bootstrap приложения), не из глобала.
Секция плагина — tool.<plugin_name>: одна и та же секция используется и для
gate (enable/tools), и для резолва всех FromConfig-типов этого плагина.
Модель про свой путь не знает — секцию даёт сборщик (plugin_name от builder).
"""

from __future__ import annotations

from omegaconf import DictConfig
from pydantic import BaseModel, ConfigDict

from boba.settings.bind import bind
from boba.settings.types import StringList

__all__ = [
    "OmegaConfPluginToolFilter",
    "OmegaConfResolver",
    "PluginConfigBase",
    "plugin_section",
]


def plugin_section(plugin_name: str) -> str:
    """Dotted-path секции плагина (общая для gate и резолва конфигов)."""
    return f"tool.{plugin_name}"


class PluginConfigBase(BaseModel):
    """Meta-config плагина: что framework читает из [tool.<plugin_name>]."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList | None = None


class OmegaConfResolver:
    """ConfigResolver поверх конфиг-инстанса: тип резолвится из секции плагина."""

    def __init__(self, config: DictConfig) -> None:
        self._config = config

    def resolve(self, cfg_type: type, plugin_name: str) -> object:
        return bind(self._config, plugin_section(plugin_name), cfg_type)


class OmegaConfPluginToolFilter:
    """PluginToolFilter поверх конфиг-инстанса: фильтрует по [tool.<name>].

    - enable=false (или секции нет) — плагин выключен;
    - enable=true без tools — допускаются все tool'ы плагина;
    - enable=true + tools=[...] — только перечисленные (по wire-имени).
    """

    def __init__(self, config: DictConfig) -> None:
        self._config = config

    def check_plugin_name(self, plugin_name: str) -> bool:
        return self._meta(plugin_name).enable

    def check_tool(self, plugin_name: str, tool_name: str) -> bool:
        meta = self._meta(plugin_name)
        if not meta.enable:
            return False
        if meta.tools is None:
            return True
        return tool_name in meta.tools

    def _meta(self, plugin_name: str) -> PluginConfigBase:
        return bind(self._config, plugin_section(plugin_name), PluginConfigBase)

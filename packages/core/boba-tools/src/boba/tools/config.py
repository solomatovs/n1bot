"""
Порты конфигурации tool-слоя: резолв `FromConfig`-типов и фильтрация плагинов

`ToolBuilder` зависит только от этих абстракций, а не от конкретного источника
конфигурации. Это разводит две ответственности:

- `ConfigResolver` — «тип → готовый инстанс конфига» (для `FromConfig`-параметров);
- `PluginToolFilter` — «какие плагины и их tool'ы попадают в реестр».

Конкретные реализации поверх `boba.settings.ConfigSource` живут в composition-
слое (`boba.agent`), а не здесь — boba-tools не зависит от boba-settings.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "ConfigResolver",
    "PluginFilterAllowAll",
    "PluginToolAllowListFilter",
    "PluginToolFilter",
]


@runtime_checkable
class ConfigResolver(Protocol):
    """Резолвит `FromConfig`-тип в готовый инстанс конфига."""

    def resolve(self, cfg_type: type) -> object:
        """Вернуть инстанс `cfg_type`, наполненный из источника."""
        ...


@runtime_checkable
class PluginToolFilter(Protocol):
    """Решает, какой плагин включён и какие его tool'ы допустимы.

    Два эшелона, в порядке вызова из `discover_plugins`:
    1. `check_plugin_name(plugin_name)` — пропускаем ли плагин целиком;
    2. `check_tool(plugin_name, tool_name)` — каждый tool пропущенного плагина.
    """

    def check_plugin_name(self, plugin_name: str) -> bool:
        """Вернуть, включён ли плагин `plugin_name`."""
        ...

    def check_tool(self, plugin_name: str, tool_name: str) -> bool:
        """Вернуть, допустим ли tool `tool_name` из плагина `plugin_name`."""
        ...


class PluginFilterAllowAll(PluginToolFilter):
    """Фильтр, пропускающий все плагины и все их tool'ы."""

    def check_plugin_name(self, plugin_name: str) -> bool:
        return True

    def check_tool(self, plugin_name: str, tool_name: str) -> bool:
        return True


class PluginToolAllowListFilter(PluginToolFilter):
    """Фильтр одного плагина: пропускает только tool'ы из его allowlist."""

    def __init__(self, plugin_name: str, allowlist: list[str]) -> None:
        self._plugin_name = plugin_name
        self._allowlist = allowlist

    def check_plugin_name(self, plugin_name: str) -> bool:
        return plugin_name == self._plugin_name

    def check_tool(self, plugin_name: str, tool_name: str) -> bool:
        return plugin_name == self._plugin_name and tool_name in self._allowlist

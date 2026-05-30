"""ConfigSource-адаптеры портов tool-слоя.

`boba-tools` определяет порты `ConfigResolver`/`PluginToolFilter`, но намеренно
не знает про `boba.settings`. Здесь — конкретные реализации поверх `ConfigSource`
и дефолты на `TomlEnvConfigSource`. Это composition-слой: tool-фреймворк
остаётся независимым от источника конфигурации, а связку «порт ↔ ConfigSource»
держит agent-слой.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from boba.settings import ConfigPath, ConfigSource, StringList, TomlEnvConfigSource
from boba.tools import ConfigResolver, PluginToolFilter

__all__ = [
    "ConfigSourcePluginToolFilter",
    "ConfigSourceResolver",
    "PluginConfigBase",
    "default_config_source",
]


class PluginConfigBase(BaseModel):
    """Meta-config плагина: что framework читает из `[tool.<plugin_name>]`."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = False
    tools: StringList | None = None


class ConfigSourceResolver(ConfigResolver):
    """`ConfigResolver` поверх `ConfigSource`: путь из `config_path` + validate."""

    def __init__(self, source: ConfigSource) -> None:
        self._source = source

    def resolve(self, cfg_type: type) -> object:
        path = self._config_path_for(cfg_type)
        return cfg_type.model_validate(self._source.for_path(path))

    @staticmethod
    def _config_path_for(cfg_type: type) -> ConfigPath:
        mc = getattr(cfg_type, "model_config", {})
        section = mc.get("config_path", "") if isinstance(mc, dict) else ""
        if isinstance(section, str):
            return tuple(s for s in section.split(".") if s)
        return tuple(section)


class ConfigSourcePluginToolFilter(PluginToolFilter):
    """`PluginToolFilter` поверх `ConfigSource`: фильтрует по `[tool.<name>]`.

    Семантика секции `[tool.<plugin_name>]`:
    - `enable=false` (или секции нет) — плагин выключен (`check_plugin_name`);
    - `enable=true` без `tools` — допускаются все tool'ы плагина;
    - `enable=true` + `tools=[...]` — только перечисленные (по wire-имени).
    """

    def __init__(self, source: ConfigSource) -> None:
        self._source = source

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
        return PluginConfigBase.model_validate(
            self._source.for_path(("tool", plugin_name))
        )


def default_config_source() -> ConfigSource:
    """`ConfigSource` по умолчанию — TOML+env (`TomlEnvConfigSource`).

    Приложение создаёт источник один раз и прокидывает в
    `ConfigSourceResolver`/`ConfigSourcePluginToolFilter`.
    """
    return TomlEnvConfigSource()

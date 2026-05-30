"""Тесты ConfigSource-адаптеров портов tool-слоя (`boba.agent.tool_config`).

Парсинг `[tool.<name>]` (enable/tools, в т.ч. из env-строк) и резолв
`FromConfig`-типа по `config_path` — ответственность adapter-слоя, отделённого
от boba-tools.
"""

from __future__ import annotations

from boba.agent.tool_config import (
    ConfigSourcePluginToolFilter,
    ConfigSourceResolver,
    default_config_source,
)
from boba.settings import (
    BobaFlatSettings,
    BobaSettingsConfigDict,
    ConfigSource,
    DictConfigSource,
)


class _Cfg(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(config_path="tool.demo")

    n: int = 0
    label: str = "default"


# --- ConfigSourcePluginToolFilter ----------------------------------------- #


def test_filter_disabled_when_missing():
    """`enable=false` (или секции нет) — плагин выключен, tool'ы не проходят."""
    flt = ConfigSourcePluginToolFilter(DictConfigSource({}))
    assert flt.check_plugin_name("myplug") is False
    assert flt.check_tool("myplug", "a") is False


def test_filter_enabled_without_allowlist_passes_all():
    flt = ConfigSourcePluginToolFilter(
        DictConfigSource({"tool.myplug": {"enable": True}})
    )
    assert flt.check_plugin_name("myplug") is True
    assert flt.check_tool("myplug", "a") is True
    assert flt.check_tool("myplug", "b") is True


def test_filter_bool_string_enable_parsed():
    """`enable="true"` (как из env-var) парсится в True."""
    flt = ConfigSourcePluginToolFilter(
        DictConfigSource({"tool.myplug": {"enable": "true"}})
    )
    assert flt.check_plugin_name("myplug") is True


def test_filter_csv_string_tools_allowlist():
    """`tools` как CSV-строка (как из env-var) парсится в allowlist."""
    flt = ConfigSourcePluginToolFilter(
        DictConfigSource({"tool.myplug": {"enable": True, "tools": "a,b"}})
    )
    assert flt.check_tool("myplug", "a") is True
    assert flt.check_tool("myplug", "b") is True
    assert flt.check_tool("myplug", "c") is False


def test_filter_list_tools_allowlist():
    flt = ConfigSourcePluginToolFilter(
        DictConfigSource({"tool.myplug": {"enable": True, "tools": ["a"]}})
    )
    assert flt.check_tool("myplug", "a") is True
    assert flt.check_tool("myplug", "b") is False


# --- ConfigSourceResolver ------------------------------------------------- #


def test_resolver_reads_config_path():
    resolver = ConfigSourceResolver(
        DictConfigSource({"tool.demo": {"n": 7, "label": "hi"}})
    )
    cfg = resolver.resolve(_Cfg)
    assert isinstance(cfg, _Cfg)
    assert (cfg.n, cfg.label) == (7, "hi")


def test_resolver_defaults_when_section_missing():
    resolver = ConfigSourceResolver(DictConfigSource({}))
    cfg = resolver.resolve(_Cfg)
    assert isinstance(cfg, _Cfg)
    assert (cfg.n, cfg.label) == (0, "default")


# --- defaults ------------------------------------------------------------- #


def test_default_config_source_returns_config_source():
    assert isinstance(default_config_source(), ConfigSource)

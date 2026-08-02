"""tool_config: ре-экспорт OmegaConf-резолверов в composition-слой агента.

Детальная логика резолва/фильтрации покрыта в boba-settings/tests/test_resolver.py;
здесь — что composition-слой отдаёт рабочие реализации портов поверх конфиг-инстанса.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from boba.agent.tool_config import (
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
    build_app_config,
)
from boba.settings import ConfigBuilder
from boba.tools import ConfigResolver, PluginToolFilter


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    n: int = 0
    label: str = "default"


def test_reexports_implement_tool_ports() -> None:
    config = ConfigBuilder().add_dict({"tool": {"demo": {}}}).build()
    assert isinstance(OmegaConfResolver(config), ConfigResolver)
    assert isinstance(OmegaConfPluginToolFilter(config), PluginToolFilter)


def test_build_app_config_available() -> None:
    assert callable(build_app_config)


def test_resolver_reads_plugin_section() -> None:
    config = ConfigBuilder().add_dict({"tool": {"demo": {"n": 7, "label": "hi"}}}).build()
    cfg = OmegaConfResolver(config).resolve(_Cfg, "demo")
    assert isinstance(cfg, _Cfg)
    assert (cfg.n, cfg.label) == (7, "hi")


def test_filter_enable_and_allowlist() -> None:
    config = ConfigBuilder().add_dict(
        {"tool": {"myplug": {"enable": True, "tools": ["a"]}}}
    ).build()
    flt = OmegaConfPluginToolFilter(config)
    assert flt.check_plugin_name("myplug") is True
    assert flt.check_tool("myplug", "a") is True
    assert flt.check_tool("myplug", "b") is False

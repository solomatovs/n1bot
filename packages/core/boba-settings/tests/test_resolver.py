"""resolver: тип резолвится из секции плагина `tool.<plugin>` явного инстанса."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from boba.settings import (
    ConfigBuilder,
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
    plugin_section,
)


class _PgCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    max_rows: int = 100


def test_plugin_section() -> None:
    assert plugin_section("pg") == "tool.pg"


def test_resolver_reads_plugin_section() -> None:
    cfg = ConfigBuilder().add_dict({"tool": {"pg": {"max_rows": 42}}}).build()
    got = OmegaConfResolver(cfg).resolve(_PgCfg, "pg")
    assert isinstance(got, _PgCfg)
    assert got.max_rows == 42

def test_plugin_filter_disabled_by_default() -> None:
    cfg = ConfigBuilder().add_dict({"tool": {"web": {}}}).build()
    f = OmegaConfPluginToolFilter(cfg)
    assert f.check_plugin_name("web") is False
    assert f.check_tool("web", "web_fetch") is False


def test_plugin_filter_enabled_all_tools() -> None:
    cfg = ConfigBuilder().add_dict({"tool": {"web": {"enable": True}}}).build()
    f = OmegaConfPluginToolFilter(cfg)
    assert f.check_plugin_name("web") is True
    assert f.check_tool("web", "anything") is True


def test_plugin_filter_allowlist() -> None:
    cfg = ConfigBuilder().add_dict(
        {"tool": {"web": {"enable": True, "tools": ["web_fetch", "web_grep"]}}}
    ).build()
    f = OmegaConfPluginToolFilter(cfg)
    assert f.check_tool("web", "web_fetch") is True
    assert f.check_tool("web", "web_download") is False

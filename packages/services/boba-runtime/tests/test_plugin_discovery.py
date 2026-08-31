"""Обнаружение tool-плагинов по entry points и требование конфига плагина."""

from __future__ import annotations

import pytest
from omegaconf import OmegaConf

from boba.access import GrantCheck
from boba.runtime.plugins import EntryPointPlugins, ToolLoader, ToolPlugin
from boba.stand.refs import StandRefs

EXPECTED = {"bash", "ch", "chart", "confluence", "doc", "ingest", "kb", "pg", "web"}


def test_installed_packages_are_discovered() -> None:
    table = EntryPointPlugins.discover()

    assert set(table) >= EXPECTED

    for plugin in table.values():
        assert plugin.discovered


def test_connected_plugins_carry_the_spec() -> None:
    table = EntryPointPlugins.discover()

    for section in ("pg", "ch", "web"):
        assert table[section].connections is not None

    for section in ("doc", "chart", "kb"):
        assert table[section].connections is None


def test_bash_plugin_builds_by_factory() -> None:
    table = EntryPointPlugins.discover()

    bash = table["bash"]
    assert bash.build is not None
    assert bash.config_model is not None


def test_discovered_plugin_without_config_file_refuses_start() -> None:
    raw = OmegaConf.create({"tool_launcher": {"provider": "sandbox"}})
    plugins = {"pg": ToolPlugin(section="pg", discovered=True)}
    loader = ToolLoader(raw, plugins, StandRefs.none(), GrantCheck.HOSTED)

    with pytest.raises(RuntimeError, match=r"conf/plugins/pg\.toml is missing"):
        loader.load()

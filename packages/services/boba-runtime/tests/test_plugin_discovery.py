"""Обнаружение tool-плагинов по entry points и требование конфига плагина."""

from __future__ import annotations

import pytest
from omegaconf import OmegaConf

from boba.access import GrantCheck
from boba.runtime.plugins import EntryPointPlugins, ToolLoader, ToolPlugin
from boba.stand.refs import StandRefs
from boba.toolkit.entry import ToolArgv
from boba.toolrun.wrapping import ToolSchema

EXPECTED = {"bash", "ch", "chart", "confluence", "doc", "ingest", "kb", "pg", "web"}


def test_installed_packages_are_discovered() -> None:
    table = EntryPointPlugins.discover()

    assert set(table) >= EXPECTED

    for plugin in table.values():
        assert plugin.discovered


def test_connection_parameters_are_declared_by_the_tools() -> None:
    """Соединения объявляет подпись инструмента, а не манифест плагина."""
    table = EntryPointPlugins.discover()

    for section in ("pg", "ch", "web"):
        assert _takes_connections(table[section])

    for section in ("doc", "chart", "kb"):
        assert not _takes_connections(table[section])


def _takes_connections(plugin: ToolPlugin) -> bool:
    for tool in plugin.module_tools:
        schema = ToolSchema.of(tool)
        if schema is None:
            continue

        if ToolArgv.connection_fields(schema):
            return True

    return False


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

"""Обнаружение tool-плагинов по entry points и требование конфига плагина."""

from __future__ import annotations

import pytest
from omegaconf import OmegaConf

from boba.access import GrantCheck
from boba.runtime.plugins import (
    ConfigGrants,
    EntryPointPlugins,
    ToolLoader,
    ToolPlugin,
)
from boba.stand.refs import StandRefs
from boba.stand_core.context import CallStand
from boba.toolkit.entry import ToolArgv

EXPECTED = {"bash", "ch", "chart", "confluence", "doc", "ingest", "kb", "pg", "web"}


def test_installed_packages_are_discovered() -> None:
    table = EntryPointPlugins().discover()

    assert set(table) >= EXPECTED

    for plugin in table.values():
        assert plugin.package


def test_connection_parameters_are_declared_by_the_tools() -> None:
    """Соединения объявляет подпись инструмента, а не манифест плагина."""
    table = EntryPointPlugins().discover()

    for section in ("pg", "ch", "web"):
        assert _takes_connections(table[section])

    for section in ("doc", "chart", "kb"):
        assert not _takes_connections(table[section])


def _takes_connections(plugin: ToolPlugin) -> bool:
    for tool in plugin.module_tools:
        if ToolArgv.connection_fields(tool.args_schema):
            return True

    return False


def test_bash_plugin_is_a_module_tool() -> None:
    table = EntryPointPlugins().discover()

    bash = table["bash"]
    assert [tool.name for tool in bash.module_tools] == ["bash"]
    assert bash.modules == ("boba.tool.shell.tools",)


def test_discovered_plugin_without_config_file_refuses_start(
    runtime_stand: StandRefs,
    call_stand: CallStand,
) -> None:
    raw = OmegaConf.create(
        {
            "tool_launcher": {"provider": "sandbox"},
            "stream_groups": {
                "open_sec": 60.0,
                "stall_sec": 120.0,
                "poll_sec": 0.2,
                "pipe_bytes": 65536,
                "pipe_bytes_max": 1 << 30,
            },
        }
    )
    plugins = {"pg": ToolPlugin(section="pg")}
    loader = ToolLoader(
        raw,
        plugins,
        runtime_stand.none().execution(),
        runtime_stand.launchers(raw),
        ConfigGrants(raw).grants(GrantCheck.HOSTED),
    )

    with pytest.raises(RuntimeError, match=r"conf/plugins/pg\.toml is missing"):
        loader.load()

"""Плагины сервиса: id из секций [tool.*], манифесты от программ плагинов в
обоих способах запуска; плагин без своего корня или с непригодной секцией не
поднимается, а остальные стартуют."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pytest
from omegaconf import DictConfig

from boba.config import ConfigError
from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand.toolsetup import ToolSetup
from boba.stand_core.context import CallStand
from boba.toolkit.manifest import InjectedRole
from boba.toolrun.dev_null import DevNullTool
from boba.toolrun.plugins import PluginKits, ToolLoader

pytestmark = pytest.mark.anyio

ENABLED = ("bash", "canvas", "ch", "doc", "pg")
"""Плагины без хранилища: их старт — только манифест, setup не нужен."""


def _enabled_only(
    raw: DictConfig, enabled: Iterable[str], launcher: str
) -> dict[str, object]:
    """Изменения конфига сервиса: включены только плагины enabled, способ
    запуска — launcher."""
    kept = frozenset(enabled)
    changes: dict[str, object] = {"tool_launcher.provider": launcher}
    for plugin_id in raw["tool"]:
        changes[f"tool.{plugin_id}.enable"] = plugin_id in kept

    return changes


@pytest.mark.parametrize("launcher", ["process", "sandbox"])
async def test_manifests_come_from_the_plugin_programs(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
    launcher: str,
) -> None:
    raw = config_overlay.tree(_enabled_only(raw_config, ENABLED, launcher))

    kits = await PluginKits(raw, runtime_stand.launchers(raw)).load()

    assert set(kits) == set(ENABLED)

    pg = kits["pg"]
    assert pg.launch.manifest.id == "pg"
    assert pg.launch.manifest.network
    tools = {decl.name: decl for decl in pg.launch.manifest.tools}
    assert tools["pg_query"].connections == {"connection": "postgres"}
    assert "sql" in tools["pg_query"].properties()
    assert pg.section.values["max_rows"] == raw_config["tool"]["pg"]["max_rows"]

    canvas = {decl.name: decl for decl in kits["canvas"].launch.manifest.tools}
    assert canvas["canvas_open"].injected["subject"] is InjectedRole.SUBJECT


async def test_plugin_without_its_root_is_left_out(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
) -> None:
    """Корень называет конфиг плагина: нет файла — не поднимается только этот
    плагин, и журнал старта называет путь."""
    changes = ToolSetup.sandbox_changes()
    changes.update(_enabled_only(raw_config, ("pg", "bash"), "sandbox"))
    changes["tool.pg.sandbox.rootfs"] = "rootfs/nope.ext4"
    raw = config_overlay.tree(changes)

    with pytest.raises(ConfigError, match="loading plugin 'pg' failed") as caught:
        await PluginKits(raw, runtime_stand.launchers(raw)).load()

    assert "nope.ext4" in str(caught.value)


@pytest.mark.parametrize("launcher", ["process", "sandbox"])
@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"tool.pg.tools": []},
        {"tool.pg.tools": ["pg_query"]},
        {"tool.pg.tools": ["pg_query", "pg_nope"]},
    ],
    ids=["no-list", "empty-list", "partial-list", "unknown-name"],
)
@pytest.mark.usefixtures("call_stand")
async def test_enabled_plugin_loads_all_manifest_tools(
    runtime_stand: StandRefs,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
    launcher: str,
    selection: Mapping[str, object],
) -> None:
    changes = ToolSetup.sandbox_changes()
    changes.update(_enabled_only(raw_config, ("pg",), launcher))
    changes.update(selection)
    raw = config_overlay.tree(changes)
    launchers = runtime_stand.launchers(raw)
    kits = await PluginKits(raw, launchers).load()

    assert set(kits) == {"pg"}

    loader = ToolLoader(
        raw,
        kits,
        runtime_stand.execution(),
        launchers,
        StandRefs.granted("all", ["*"]),
    )
    registry = loader.load()
    declared = [decl.name for decl in kits["pg"].launch.manifest.tools]
    loaded = [tool.name for tool in registry.tools if tool.name != DevNullTool.NAME]

    assert len(declared) > 1
    assert loaded == declared


async def test_plugin_section_is_checked_by_the_plugin(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
) -> None:
    """Ключи секции сверх ключей фреймворка проверяет сам плагин: сервер их
    не знает, а непригодную секцию отвергает программа плагина, и этот плагин
    не поднимается."""
    changes = ToolSetup.sandbox_changes()
    changes.update(_enabled_only(raw_config, ("pg",), "sandbox"))
    changes["tool.pg.max_rows"] = 0
    raw = config_overlay.tree(changes)

    with pytest.raises(ConfigError, match="loading plugin 'pg' failed") as caught:
        await PluginKits(raw, runtime_stand.launchers(raw)).load()

    assert "does not fit PgToolConfig" in str(caught.value)

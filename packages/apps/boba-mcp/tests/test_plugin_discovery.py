"""Плагины сервиса: id из секций [tool.*], манифесты от программ плагинов в
обоих способах запуска; плагин без своего корня или с непригодной секцией не
поднимается, а остальные стартуют."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from omegaconf import DictConfig

from boba.runtime.plugins import PluginKits
from boba.stand.overlay import ConfigOverlay
from boba.stand.refs import StandRefs
from boba.stand.toolsetup import ToolSetup
from boba.stand_core.context import CallStand
from boba.toolkit.manifest import InjectedRole
from boba.toolrun.injected import StaticSection

pytestmark = pytest.mark.anyio

ENABLED = ("bash", "canvas", "ch", "doc", "pg")
"""Плагины без хранилища: их старт — только манифест, setup не нужен."""


def _enabled_only(
    raw: DictConfig, enabled: Iterable[str], launcher: str
) -> dict[str, object]:
    """Изменения конфига сервиса: включены только плагины enabled, способ
    запуска — launcher."""
    kept = frozenset(enabled)
    changes: dict[str, object] = {"env.tool_launcher": launcher}
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

    kits = await PluginKits(raw, runtime_stand.launchers(raw), StaticSection()).load()

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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Корень называет конфиг плагина: нет файла — не поднимается только этот
    плагин, и журнал старта называет путь."""
    changes = ToolSetup.sandbox_changes()
    changes.update(_enabled_only(raw_config, ("pg", "bash"), "sandbox"))
    changes["tool.pg.sandbox.rootfs"] = "rootfs/nope.ext4"
    raw = config_overlay.tree(changes)

    kits = await PluginKits(raw, runtime_stand.launchers(raw), StaticSection()).load()

    assert set(kits) == {"bash"}
    assert "plugin pg is not started" in caplog.text
    assert "nope.ext4" in caplog.text


async def test_tool_the_plugin_does_not_declare_refuses_start(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
) -> None:
    changes = _enabled_only(raw_config, ("pg",), "process")
    changes["tool.pg.tools"] = ["pg_query", "pg_nope"]
    raw = config_overlay.tree(changes)

    with pytest.raises(RuntimeError, match=r"\[tool\.pg\] tools names \['pg_nope'\]"):
        await runtime_stand.registry(
            raw, runtime_stand.none(), StandRefs.granted("all", ["*"])
        )


async def test_plugin_section_is_checked_by_the_plugin(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ключи секции сверх ключей фреймворка проверяет сам плагин: сервер их
    не знает, а непригодную секцию отвергает программа плагина, и этот плагин
    не поднимается."""
    changes = ToolSetup.sandbox_changes()
    changes.update(_enabled_only(raw_config, ("pg",), "sandbox"))
    changes["tool.pg.max_rows"] = 0
    raw = config_overlay.tree(changes)

    kits = await PluginKits(raw, runtime_stand.launchers(raw), StaticSection()).load()

    assert "pg" not in kits
    assert "does not fit PgToolConfig" in caplog.text

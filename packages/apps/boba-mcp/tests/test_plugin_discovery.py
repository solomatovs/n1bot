"""Комплекты плагинов сервиса: id из секций [tool.*], манифесты от программ
плагинов в обоих способах запуска и отказы старта на неполном комплекте."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from omegaconf import DictConfig, OmegaConf

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


async def test_config_without_image_refuses_start(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
) -> None:
    changes = _enabled_only(raw_config, ("pg",), "sandbox")
    changes["tool.nope"] = {"enable": False}
    raw = config_overlay.tree(changes)

    loading = PluginKits(raw, runtime_stand.launchers(raw), StaticSection())

    with pytest.raises(RuntimeError, match=r"conf/plugins/nope\.toml.*nope\.ext4"):
        await loading.load()


async def test_image_without_config_refuses_start(
    runtime_stand: StandRefs,
    call_stand: CallStand,
    raw_config: DictConfig,
    config_overlay: ConfigOverlay,
) -> None:
    changes = _enabled_only(raw_config, ("pg",), "sandbox")
    enabled = config_overlay.tree(changes)
    plugins = OmegaConf.to_container(enabled["tool"], resolve=False)
    assert isinstance(plugins, dict)
    plugins.pop("mail")
    changes.update(
        config_overlay.replacement("tool", {str(k): v for k, v in plugins.items()})
    )
    raw = config_overlay.tree(changes)

    loading = PluginKits(raw, runtime_stand.launchers(raw), StaticSection())

    with pytest.raises(RuntimeError, match=r"mail\.ext4 has no config"):
        await loading.load()


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
) -> None:
    """Ключи секции сверх ключей фреймворка проверяет сам плагин: сервер их
    не знает, а непригодную секцию отвергает программа плагина."""
    changes = ToolSetup.sandbox_changes()
    changes.update(_enabled_only(raw_config, ("pg",), "sandbox"))
    changes["tool.pg.max_rows"] = 0
    raw = config_overlay.tree(changes)

    loading = PluginKits(raw, runtime_stand.launchers(raw), StaticSection())

    with pytest.raises(
        RuntimeError, match=r"(?s)`manifest`.*does not fit PgToolConfig"
    ):
        await loading.load()

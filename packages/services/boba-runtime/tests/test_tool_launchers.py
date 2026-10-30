"""Выбор способа запуска по [tool_launcher], проверки старта реализаций и
подкоманды программы плагина: манифест от плагина из окружения процесса."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from boba.config import ConfigError
from boba.runtime.launchers import (
    NoLaunchers,
    PluginLaunch,
    ProcessLaunchers,
    ToolLaunchers,
    ZygoteLaunchers,
)
from boba.stand_core.context import CallStand
from boba.stand_core.fake_toolmod import PLUGIN
from boba.toolkit.chain import CallAmbient
from boba.toolkit.manifest import ManifestBuilder
from boba.toolrun.process import ProcessToolCaller

FAKE_LAUNCH = PluginLaunch(manifest=ManifestBuilder().manifest(PLUGIN, "0"), section={})
"""Запуск фейкового плагина: манифест его объявления и пустая секция."""

PG_SECTION: dict[str, object] = {
    "enable": True,
    "max_rows": 500,
    "max_bytes": 1_000_000,
    "tools": ["pg_query"],
}
"""Секция плагина pg, как её даёт conf/plugins/pg.toml."""


def _process_section(workdir: Path) -> dict[str, object]:
    return {
        "tool_launcher": {
            "provider": "process",
            "workdir": str(workdir),
            "timeout_sec": 60,
            "channel_limit_bytes": 1_000_000,
            "stderr_tail_bytes": 4096,
            "kill_grace_sec": 1.0,
        }
    }


def test_process_without_the_section_launches_no_tool_sections(
    call_stand: CallStand,
) -> None:
    raw = OmegaConf.create({})

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, NoLaunchers)

    launchers.probe()
    with pytest.raises(
        RuntimeError, match=r"'fake' cannot be launched.*\[tool_launcher\]"
    ):
        launchers.launcher_of(FAKE_LAUNCH, call_stand.contexts, CallAmbient())


def _sandbox_section() -> dict[str, object]:
    """Способ запуска песочницей; интерпретатор лаунчер снимает с процесса сам."""
    return {"tool_launcher": {"provider": "sandbox"}}


def test_sandbox_provider_builds_zygote_launchers() -> None:
    raw = OmegaConf.create(_sandbox_section())

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, ZygoteLaunchers)


def test_sandbox_probe_requires_env_paths() -> None:
    raw = OmegaConf.create(_sandbox_section())

    launchers = ToolLaunchers(raw).build()

    # конвенции песочницы стоят на путях [env]: без них probe отказывает
    with pytest.raises(ConfigError, match=r"section \[env\] is missing"):
        launchers.probe()


def test_process_provider_builds_process_launchers(
    call_stand: CallStand, tmp_path: Path
) -> None:
    raw = OmegaConf.create(_process_section(tmp_path))

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, ProcessLaunchers)

    launchers.probe()
    launcher = launchers.launcher_of(FAKE_LAUNCH, call_stand.contexts, CallAmbient())

    assert isinstance(launcher, ProcessToolCaller)


def test_process_probe_requires_existing_workdir(tmp_path: Path) -> None:
    raw = OmegaConf.create(_process_section(tmp_path / "absent"))

    launchers = ToolLaunchers(raw).build()

    with pytest.raises(RuntimeError, match="workdir"):
        launchers.probe()


def test_process_manifest_comes_from_the_plugin_program(tmp_path: Path) -> None:
    """Манифест отдаёт программа плагина своим процессом: сервер получает
    данные, а не модули плагина."""
    launchers = ToolLaunchers(OmegaConf.create(_process_section(tmp_path))).build()

    manifest = launchers.manifest_of("pg", PG_SECTION)

    assert manifest.id == "pg"
    assert manifest.network
    names = {tool.name for tool in manifest.tools}
    assert "pg_query" in names
    query = next(tool for tool in manifest.tools if tool.name == "pg_query")
    assert query.connections == {"connection": "postgres"}


def test_process_manifest_refuses_a_section_the_plugin_rejects(
    tmp_path: Path,
) -> None:
    """Секцию проверяет сам плагин: отказ старта несёт его текст и код."""
    launchers = ToolLaunchers(OmegaConf.create(_process_section(tmp_path))).build()

    with pytest.raises(RuntimeError, match=r"`manifest`.*code 3.*PgToolConfig"):
        launchers.manifest_of("pg", {"max_rows": 0})


def test_process_manifest_of_an_unknown_plugin_is_refused(tmp_path: Path) -> None:
    launchers = ToolLaunchers(OmegaConf.create(_process_section(tmp_path))).build()

    with pytest.raises(RuntimeError, match="'nope' is not installed"):
        launchers.manifest_of("nope", {})


def test_no_launchers_refuse_the_manifest() -> None:
    launchers = ToolLaunchers(OmegaConf.create({})).build()

    with pytest.raises(RuntimeError, match="'pg' cannot be launched"):
        launchers.manifest_of("pg", PG_SECTION)


def _env_section(sandbox: Path) -> dict[str, object]:
    """[env] песочницы: каталог образов плагинов sandbox, остальное — пустышки."""
    paths: dict[str, object] = {}
    for key in ("base", "data", "third", "models", "krb", "cgroup_base"):
        paths[key] = str(sandbox / key)

    return {**_sandbox_section(), "env": {**paths, "sandbox": str(sandbox)}}


def test_sandbox_config_without_image_is_refused(tmp_path: Path) -> None:
    (tmp_path / "plugins").mkdir()
    launchers = ToolLaunchers(OmegaConf.create(_env_section(tmp_path))).build()

    with pytest.raises(RuntimeError, match=r"conf/plugins/pg.toml.*pg\.ext4"):
        launchers.check_kits(["pg"])


def test_sandbox_image_without_config_is_refused(tmp_path: Path) -> None:
    images = tmp_path / "plugins"
    images.mkdir()
    (images / "pg.ext4").write_bytes(b"")
    (images / "mail.ext4").write_bytes(b"")
    launchers = ToolLaunchers(OmegaConf.create(_env_section(tmp_path))).build()

    with pytest.raises(RuntimeError, match=r"mail\.ext4 has no config"):
        launchers.check_kits(["pg"])

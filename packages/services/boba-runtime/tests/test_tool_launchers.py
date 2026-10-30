"""Выбор способа запуска по [tool_launcher], проверки старта реализаций и
подкоманды программы плагина: манифест от плагина из окружения процесса."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from boba.config import ConfigError
from boba.runtime.config import ProgramRoots
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


def _instance(data_dir: Path) -> dict[str, object]:
    return {"data_dir": str(data_dir), "ca_file": "/etc/ssl/certs/ca-certificates.crt"}


def _process_section(data_dir: Path) -> dict[str, object]:
    """Способ запуска процессом; workdir — workspace/ каталога данных."""
    (data_dir / "workspace").mkdir(parents=True, exist_ok=True)

    return {
        "instance": _instance(data_dir),
        "tool_launcher": {
            "provider": "process",
            "timeout_sec": 60,
            "channel_limit_bytes": 1_000_000,
            "stderr_tail_bytes": 4096,
            "kill_grace_sec": 1.0,
        },
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


def _sandbox_section(data_dir: Path) -> dict[str, object]:
    """Способ запуска песочницей; пути плагинов — в их конфигах."""
    return {
        "instance": _instance(data_dir),
        "tool_launcher": {"provider": "sandbox", "cgroup_base": str(data_dir)},
    }


@pytest.fixture
def program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Каталог программы теста (BOBA_RUNTIME): пустой runtime/ в дереве
    исходников."""
    runtime = tmp_path / "tree" / "runtime"
    runtime.mkdir(parents=True)
    (tmp_path / "tree" / "packages").mkdir()
    monkeypatch.setenv(ProgramRoots.RUNTIME_ENV, str(runtime))

    return runtime


def test_sandbox_provider_builds_zygote_launchers(
    program: Path, tmp_path: Path
) -> None:
    raw = OmegaConf.create(_sandbox_section(tmp_path))

    launchers = ToolLaunchers(raw).build()

    assert isinstance(launchers, ZygoteLaunchers)


def test_sandbox_probe_needs_no_plugin_directory(program: Path, tmp_path: Path) -> None:
    """Пути плагинов называют их конфиги: способ запуска каталога образов не
    ищет."""
    raw = OmegaConf.create(_sandbox_section(tmp_path))

    launchers = ToolLaunchers(raw).build()

    launchers.probe()


def test_sandbox_without_cgroup_base_is_refused(program: Path, tmp_path: Path) -> None:
    raw = OmegaConf.create(
        {"instance": _instance(tmp_path), "tool_launcher": {"provider": "sandbox"}}
    )

    with pytest.raises(ConfigError, match="cgroup_base"):
        ToolLaunchers(raw).build()


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
    section = _process_section(tmp_path)
    section["instance"] = _instance(tmp_path / "absent")
    raw = OmegaConf.create(section)

    launchers = ToolLaunchers(raw).build()

    with pytest.raises(RuntimeError, match="workspace directory"):
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


def test_sandbox_plugin_without_program_is_refused(
    program: Path, tmp_path: Path
) -> None:
    """Каталог программы и образ корня называет конфиг плагина: без них
    профиль не собирается, и отказ называет секцию."""
    section = _sandbox_section(tmp_path)
    section["tool"] = {"pg": {"sandbox": {"rootfs": "rootfs/base.ext4"}}}
    launchers = ToolLaunchers(OmegaConf.create(section)).build()

    with pytest.raises(ConfigError, match=r"(?s)tool\.pg\.sandbox.*program"):
        launchers.manifest_of("pg", PG_SECTION)

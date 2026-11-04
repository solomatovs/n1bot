"""Выбор способа запуска по [tool_launcher], проверки старта реализаций и
подкоманды программы плагина: манифест от плагина из окружения процесса."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import DictConfig, OmegaConf

from boba.config import ConfigError
from boba.config.paths import PathsConfig, TlsConfig
from boba.sandbox.paths import ProgramRoots
from boba.stand.fake_toolmod import PLUGIN
from boba.stand_core.context import CallStand
from boba.tool_io.chain import CallAmbient
from boba.toolkit.manifest import ManifestBuilder
from boba.toolrun.launchers import NoLaunchers, PluginLaunch, ToolLaunchers
from boba.toolrun.process import ProcessToolCaller
from boba.toolrun.sandbox import (
    ProcessLaunchers,
    SandboxDefaults,
    StandardToolLaunchers,
    ZygoteLaunchers,
)

FAKE_LAUNCH = PluginLaunch(manifest=ManifestBuilder().manifest(PLUGIN, "0"), section={})
"""Запуск фейкового плагина: манифест его объявления и пустая секция."""

PG_SECTION: dict[str, object] = {
    "enable": True,
    "max_rows": 500,
    "max_bytes": 1_000_000,
    "tools": ["pg_query"],
}
"""Секция плагина pg, как её даёт conf/plugins/pg.toml."""


def _paths(data_dir: Path) -> dict[str, object]:
    return {"data_dir": str(data_dir)}


def _process_section(data_dir: Path) -> dict[str, object]:
    """Способ запуска процессом; workdir — workspace/ каталога данных."""
    (data_dir / "workspace").mkdir(parents=True, exist_ok=True)

    return {
        "paths": _paths(data_dir),
        "tool_launcher": {
            "provider": "process",
            "timeout_sec": 60,
            "channel_limit_bytes": 1_000_000,
            "stderr_tail_bytes": 4096,
            "kill_grace_sec": 1.0,
        },
    }


def _build(raw: DictConfig):
    """Выбор запуска с фабриками, подключаемыми приложением."""
    return ToolLaunchers(raw, StandardToolLaunchers().registry).build()


def test_process_without_the_section_launches_no_tool_sections(
    call_stand: CallStand,
) -> None:
    raw = OmegaConf.create({})

    launchers = _build(raw)

    assert isinstance(launchers, NoLaunchers)

    launchers.probe()
    with pytest.raises(
        RuntimeError, match=r"'fake' cannot be launched.*\[tool_launcher\]"
    ):
        launchers.launcher_of(FAKE_LAUNCH, call_stand.contexts, CallAmbient())


def _sandbox_section(data_dir: Path) -> dict[str, object]:
    """Способ запуска песочницей; пути плагинов — в их конфигах."""
    return {
        "paths": _paths(data_dir),
        "tls": {},
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

    launchers = _build(raw)

    assert isinstance(launchers, ZygoteLaunchers)


@pytest.mark.parametrize("ca_file", [None, "/etc/company/ca.pem"])
def test_sandbox_network_uses_the_configured_ca_bundle(
    program: Path, tmp_path: Path, ca_file: str | None
) -> None:
    tls = TlsConfig() if ca_file is None else TlsConfig(ca_file=ca_file)
    defaults = SandboxDefaults(
        ProgramRoots().locate(), PathsConfig(data_dir=str(tmp_path)), str(tmp_path), tls
    )

    assert f"{tls.ca_file}:{SandboxDefaults.CA_FILE}" in defaults.network_binds()
    assert defaults.network_env() == {"SSL_CERT_FILE": SandboxDefaults.CA_FILE}


def test_sandbox_probe_needs_no_plugin_directory(program: Path, tmp_path: Path) -> None:
    """Пути плагинов называют их конфиги: способ запуска каталога образов не
    ищет."""
    raw = OmegaConf.create(_sandbox_section(tmp_path))

    launchers = _build(raw)

    launchers.probe()


def test_sandbox_without_cgroup_base_is_refused(program: Path, tmp_path: Path) -> None:
    raw = OmegaConf.create(
        {"paths": _paths(tmp_path), "tool_launcher": {"provider": "sandbox"}}
    )

    with pytest.raises(ConfigError, match="cgroup_base"):
        _build(raw)


def test_process_provider_builds_process_launchers(
    call_stand: CallStand, tmp_path: Path
) -> None:
    raw = OmegaConf.create(_process_section(tmp_path))

    launchers = _build(raw)

    assert isinstance(launchers, ProcessLaunchers)

    launchers.probe()
    launcher = launchers.launcher_of(FAKE_LAUNCH, call_stand.contexts, CallAmbient())

    assert isinstance(launcher, ProcessToolCaller)


def test_process_probe_requires_existing_workdir(tmp_path: Path) -> None:
    section = _process_section(tmp_path)
    section["paths"] = _paths(tmp_path / "absent")
    raw = OmegaConf.create(section)

    launchers = _build(raw)

    with pytest.raises(RuntimeError, match="workspace directory"):
        launchers.probe()


def test_process_manifest_comes_from_the_plugin_program(tmp_path: Path) -> None:
    """Манифест отдаёт программа плагина своим процессом: сервер получает
    данные, а не модули плагина."""
    launchers = _build(OmegaConf.create(_process_section(tmp_path)))

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
    launchers = _build(OmegaConf.create(_process_section(tmp_path)))

    with pytest.raises(RuntimeError, match=r"`manifest`.*code 3.*PgToolConfig"):
        launchers.manifest_of("pg", {"max_rows": 0})


def test_process_manifest_of_an_unknown_plugin_is_refused(tmp_path: Path) -> None:
    launchers = _build(OmegaConf.create(_process_section(tmp_path)))

    with pytest.raises(RuntimeError, match="'nope' is not installed"):
        launchers.manifest_of("nope", {})


def test_no_launchers_refuse_the_manifest() -> None:
    launchers = _build(OmegaConf.create({}))

    with pytest.raises(RuntimeError, match="'pg' cannot be launched"):
        launchers.manifest_of("pg", PG_SECTION)


def test_sandbox_plugin_without_program_is_refused(
    program: Path, tmp_path: Path
) -> None:
    """Каталог программы и образ корня называет конфиг плагина: без них
    профиль не собирается, и отказ называет секцию."""
    section = _sandbox_section(tmp_path)
    section["tool"] = {"pg": {"sandbox": {"rootfs": "rootfs/base.ext4"}}}
    launchers = _build(OmegaConf.create(section))

    with pytest.raises(ConfigError, match=r"(?s)tool\.pg\.sandbox.*program"):
        launchers.manifest_of("pg", PG_SECTION)

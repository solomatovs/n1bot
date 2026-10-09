"""Слои конфига процесса: вычисленный base, общий конфиг, плагины, site-файл,
BOBA_-переопределения."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf
from omegaconf.errors import InterpolationKeyError

from boba.runtime.config import (
    AppLayers,
    ConfigFiles,
    ConfigLayoutError,
    EnvOverride,
)

SITE = """
[env]
    port        = 8501
    instance_id = "node1"
    host        = "localhost"

    data = "${env.base}/data"

[site]
    pg_host = "db.example"
"""

COMMON = """
[postgres]
    host = "${site.pg_host}"
    port = "${env.port}"
"""


def _write(tmp_path: Path) -> ConfigFiles:
    """Случай запуска: site.toml в <base>/conf, общий конфиг — в другом каталоге,
    как у приложения из пакета."""
    conf = tmp_path / "conf"
    conf.mkdir()
    site = conf / "site.toml"
    site.write_text(SITE, encoding="utf-8")

    package = tmp_path / "package"
    package.mkdir()
    config = package / "config.toml"
    config.write_text(COMMON, encoding="utf-8")

    return ConfigFiles(config=config, site=site)


def test_base_computed_from_site_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for override in EnvOverride:
        monkeypatch.delenv(override.var, raising=False)

    raw = AppLayers.compose(_write(tmp_path))

    assert OmegaConf.select(raw, "env.base") == str(tmp_path)
    assert OmegaConf.select(raw, "env.data") == f"{tmp_path}/data"
    assert OmegaConf.select(raw, "env.port") == 8501
    assert OmegaConf.select(raw, "postgres.host") == "db.example"
    assert OmegaConf.select(raw, "postgres.port") == 8501


def test_plugin_files_land_under_their_tool_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for override in EnvOverride:
        monkeypatch.delenv(override.var, raising=False)

    files = _write(tmp_path)

    plugins = files.plugins_dir()
    plugins.mkdir()
    body = 'enable = true\nworkdir = "${env.data}/pg"\n[sandbox]\nprofile = "x"\n'
    (plugins / "pg.toml").write_text(body, encoding="utf-8")

    raw = AppLayers.compose(files)

    assert OmegaConf.select(raw, "tool.pg.enable") is True
    assert OmegaConf.select(raw, "tool.pg.workdir") == f"{tmp_path}/data/pg"
    assert OmegaConf.select(raw, "tool.pg.sandbox.profile") == "x"


def test_environment_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EnvOverride.PORT.var, "8601")
    monkeypatch.setenv(EnvOverride.INSTANCE_ID.var, "dev")
    monkeypatch.setenv(EnvOverride.BASE.var, "/elsewhere")

    raw = AppLayers.compose(_write(tmp_path))

    assert OmegaConf.select(raw, "env.port") == "8601"
    assert OmegaConf.select(raw, "env.instance_id") == "dev"
    assert OmegaConf.select(raw, "env.base") == "/elsewhere"
    assert OmegaConf.select(raw, "env.data") == "/elsewhere/data"


def test_host_falls_back_to_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(EnvOverride.HOST.var, raising=False)
    monkeypatch.setenv(AppLayers.HOST_FALLBACK, "node-x")

    raw = AppLayers.compose(_write(tmp_path))

    assert OmegaConf.select(raw, "env.host") == "node-x"


def test_explicit_host_beats_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(EnvOverride.HOST.var, "named")
    monkeypatch.setenv(AppLayers.HOST_FALLBACK, "node-x")

    raw = AppLayers.compose(_write(tmp_path))

    assert OmegaConf.select(raw, "env.host") == "named"


def test_override_variable_names_carry_the_prefix() -> None:
    assert EnvOverride.PORT.var == "BOBA_PORT"
    assert EnvOverride.MESSAGING.var == "BOBA_MESSAGING"
    assert EnvOverride.TOOL_LAUNCHER.var == "BOBA_TOOL_LAUNCHER"


def test_site_section_in_the_common_config_is_refused(tmp_path: Path) -> None:
    files = _write(tmp_path)
    files.config.write_text(COMMON + '\n[site]\n    pg_host = "x"\n')

    with pytest.raises(ConfigLayoutError, match="belong to the site file"):
        AppLayers.compose(files)


def test_foreign_section_in_the_site_file_is_refused(tmp_path: Path) -> None:
    files = _write(tmp_path)
    files.site.write_text(SITE + '\n[postgres]\n    host = "x"\n')

    with pytest.raises(ConfigLayoutError, match="postgres"):
        AppLayers.compose(files)


def test_missing_file_is_named(tmp_path: Path) -> None:
    files = _write(tmp_path)

    with pytest.raises(ConfigLayoutError, match="site expects an existing toml"):
        ConfigFiles(config=files.config, site=tmp_path / "absent.toml")


def test_missing_site_key_is_named_on_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for override in EnvOverride:
        monkeypatch.delenv(override.var, raising=False)

    files = _write(tmp_path)
    files.site.write_text("[env]\n    port = 8501\n", encoding="utf-8")
    raw = AppLayers.compose(files)

    with pytest.raises(InterpolationKeyError, match=r"site\.pg_host"):
        OmegaConf.to_container(raw, resolve=True)

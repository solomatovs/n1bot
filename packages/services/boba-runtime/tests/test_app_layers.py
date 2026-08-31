"""Слои конфига процесса: вычисленный base, значения toml, BOBA_-переопределения."""

from __future__ import annotations

from pathlib import Path

import pytest
from omegaconf import OmegaConf

from boba.runtime.config import AppLayers, EnvOverride

CONFIG = """
[env]
    port        = 8501
    instance_id = "node1"
    host        = "localhost"

    data = "${env.base}/data"
"""


def _write(tmp_path: Path) -> Path:
    conf = tmp_path / "conf"
    conf.mkdir()
    path = conf / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def test_base_computed_from_config_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for override in EnvOverride:
        monkeypatch.delenv(override.var, raising=False)

    raw = AppLayers.compose(_write(tmp_path))

    assert OmegaConf.select(raw, "env.base") == str(tmp_path)
    assert OmegaConf.select(raw, "env.data") == f"{tmp_path}/data"
    assert OmegaConf.select(raw, "env.port") == 8501


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

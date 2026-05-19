"""Pytest-фикстуры пакета boba-tool-shell (v2)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from boba.tool.shell.config import BashLocalConfig, BashSandboxConfig


def _clear_shell_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Снять все env-варсы, влияющие на загрузку shell-конфигов."""
    import os  # noqa: PLC0415 — локально, не дёргаем на load module

    for key in list(os.environ):
        if key.startswith("BOBA_TOOL__BASH_LOCAL__"):
            monkeypatch.delenv(key, raising=False)
        if key.startswith("BOBA_TOOL__BASH_SANDBOX__"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)


@pytest.fixture
def load_local_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[[str], BashLocalConfig]:
    """Фабрика: TOML-блок `[tool.bash_local]` → загруженный конфиг.

    На вход — тело секции _без_ заголовка `[tool.bash_local]` (он
    добавляется автоматически). Возвращает `BashLocalConfig`, прошедший
    pydantic-валидацию; падает с `ValidationError` при невалидном вводе.
    """

    def _factory(toml_body: str) -> BashLocalConfig:
        _clear_shell_env(monkeypatch)
        cfg_path = tmp_path / "boba.toml"
        cfg_path.write_text(
            "[tool.bash_local]\n" + toml_body, encoding="utf-8",
        )
        monkeypatch.setenv("BOBA_CONFIG_PATH", str(cfg_path))
        return BashLocalConfig()

    return _factory


@pytest.fixture
def load_sandbox_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Callable[[str], BashSandboxConfig]:
    """Фабрика: TOML-блок `[tool.bash_sandbox]` → загруженный конфиг."""

    def _factory(toml_body: str) -> BashSandboxConfig:
        _clear_shell_env(monkeypatch)
        cfg_path = tmp_path / "boba.toml"
        cfg_path.write_text(
            "[tool.bash_sandbox]\n" + toml_body, encoding="utf-8",
        )
        monkeypatch.setenv("BOBA_CONFIG_PATH", str(cfg_path))
        return BashSandboxConfig()

    return _factory

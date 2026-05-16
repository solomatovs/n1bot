"""Pytest-фикстуры пакета boba-tool-shell."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boba.plugin import ExtensionContext, install_plugins
from boba.tool.shell import ShellPlugin
from boba.workspace.contract import ProjectWorkspaceShell


@pytest.fixture
def ext_ctx() -> ExtensionContext:
    """`ExtensionContext` с замоканным `ProjectWorkspaceShell`."""
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BOBA_TOOL__SHELL__ENABLE",
        "BOBA_TOOL__SHELL__TOOLS",
        "BOBA_TOOL__SHELL__WORKSPACE_ROOT",
        "BOBA_TOOL__SHELL__DEFAULT_PROFILE",
        "BOBA_CONFIG_PATH",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_shell_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ext_ctx: ExtensionContext,
) -> Callable[[str], list[str]]:
    """Фабрика: TOML-тело секции `[tool.shell]` → имена tool'ов.

    На вход — содержимое TOML _без_ заголовка `[tool.shell]` (он
    добавляется автоматически). Возвращает имена tool'ов, которые
    `ShellPlugin` выдаст; падает с pydantic ValidationError /
    RuntimeError при невалидном конфиге.
    """

    def _factory(toml_body: str) -> list[str]:
        _clear_env(monkeypatch)
        cfg_path = tmp_path / "boba.toml"
        cfg_path.write_text(
            "[tool.shell]\n" + toml_body,
            encoding="utf-8",
        )
        monkeypatch.setenv("BOBA_CONFIG_PATH", str(cfg_path))
        sources = list(install_plugins([ShellPlugin], ext_ctx))
        return [t.name() for src in sources for t in src.tools()]

    return _factory


@pytest.fixture
def enabled_toml_body(tmp_path: Path) -> str:
    """Минимальный валидный TOML-блок для enable=true.

    Использует `tmp_path` как workspace_root (директория гарантированно
    существует), один профиль `default` со значениями по умолчанию.
    """
    return (
        "enable = true\n"
        f'workspace_root = "{tmp_path}"\n'
        'default_profile = "default"\n'
        "[tool.shell.profiles.default]\n"
    )

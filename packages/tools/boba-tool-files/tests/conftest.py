"""Pytest-фикстуры пакета boba-tool-files."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boba.plugin import ExtensionContext, install_plugins
from boba.tool.files import FilesPlugin
from boba.tool.files.cat import CatTool
from boba.workspace.contract import ProjectWorkspaceShell


@pytest.fixture
def ext_ctx() -> ExtensionContext:
    """`ExtensionContext` с замоканным `ProjectWorkspaceShell`."""
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


@pytest.fixture
def make_files_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    ext_ctx: ExtensionContext,
) -> Callable[[dict[str, str]], list[str]]:
    """Фабрика: env → имена tool'ов, отдаваемых `FilesPlugin`.

    Очищает `BOBA_TOOL__FILES__*` и `BOBA_CONFIG_PATH`, устанавливает
    переданный env, прогоняет `install_plugins([FilesPlugin], ...)`.
    """

    def _factory(env: dict[str, str]) -> list[str]:
        monkeypatch.delenv("BOBA_TOOL__FILES__ENABLE", raising=False)
        monkeypatch.delenv("BOBA_TOOL__FILES__TOOLS", raising=False)
        monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sources = list(install_plugins([FilesPlugin], ext_ctx))
        return [t.name() for src in sources for t in src.tools()]

    return _factory


@pytest.fixture
def make_cat_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ext_ctx: ExtensionContext,
) -> Callable[..., CatTool]:
    """Фабрика `CatTool`: TOML c включённым FilesPlugin + overlay-блок."""

    def _factory(toml_body: str = "") -> CatTool:
        cfg_path = tmp_path / "boba.toml"
        cfg_path.write_text(
            "[tool.files]\nenable = true\n" + toml_body,
            encoding="utf-8",
        )
        monkeypatch.setenv("BOBA_CONFIG_PATH", str(cfg_path))
        monkeypatch.delenv("BOBA_TOOL__FILES__ENABLE", raising=False)
        monkeypatch.delenv("BOBA_TOOL__FILES__TOOLS", raising=False)
        sources = list(install_plugins([FilesPlugin], ext_ctx))
        found = next(
            t for src in sources for t in src.tools() if t.name() == "cat"
        )
        assert isinstance(found, CatTool)
        return found

    return _factory

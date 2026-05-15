"""Pytest-фикстуры пакета boba-tool-html."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from boba.plugin import ExtensionContext, install_plugins
from boba.tool.html import HtmlPlugin
from boba.workspace.contract import ProjectWorkspaceShell


@pytest.fixture
def ext_ctx() -> ExtensionContext:
    """`ExtensionContext` с замоканным `ProjectWorkspaceShell`."""
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


@pytest.fixture
def make_html_tool_names(
    monkeypatch: pytest.MonkeyPatch,
    ext_ctx: ExtensionContext,
) -> Callable[[dict[str, str]], list[str]]:
    """Фабрика: env → имена tool'ов, отдаваемых `HtmlPlugin`.

    Очищает `BOBA_TOOL__HTML__*` и `BOBA_CONFIG_PATH`, устанавливает
    переданный env, прогоняет `install_plugins([HtmlPlugin], ...)`.
    """

    def _factory(env: dict[str, str]) -> list[str]:
        monkeypatch.delenv("BOBA_TOOL__HTML__ENABLE", raising=False)
        monkeypatch.delenv("BOBA_TOOL__HTML__TOOLS", raising=False)
        monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        sources = list(install_plugins([HtmlPlugin], ext_ctx))
        return [t.name() for src in sources for t in src.tools()]

    return _factory

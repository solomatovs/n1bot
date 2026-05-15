"""Тесты enable-конвенции и сборки HtmlPlugin."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boba.plugin import ExtensionContext, install_plugins
from boba.tool.html import HtmlPlugin
from boba.workspace.contract import ProjectWorkspaceShell


def _ext_ctx() -> ExtensionContext:
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


def _tool_names(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> list[str]:
    # Очищаем все BOBA_TOOL__HTML__* + BOBA_CONFIG_PATH, чтобы тесты не зависели
    # от внешнего окружения.
    monkeypatch.delenv("BOBA_TOOL__HTML__ENABLE", raising=False)
    monkeypatch.delenv("BOBA_TOOL__HTML__TOOLS", raising=False)
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sources = list(install_plugins([HtmlPlugin], _ext_ctx()))
    return [t.name() for src in sources for t in src.tools()]


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    assert _tool_names(monkeypatch, {}) == []


def test_disabled_explicit(monkeypatch: pytest.MonkeyPatch):
    assert _tool_names(monkeypatch, {"BOBA_TOOL__HTML__ENABLE": "false"}) == []


def test_enabled_yields_both_tools(monkeypatch: pytest.MonkeyPatch):
    names = _tool_names(monkeypatch, {"BOBA_TOOL__HTML__ENABLE": "true"})
    assert set(names) == {"html_outline", "html_section"}


def test_allowlist_filters_tools(monkeypatch: pytest.MonkeyPatch):
    names = _tool_names(
        monkeypatch,
        {
            "BOBA_TOOL__HTML__ENABLE": "true",
            "BOBA_TOOL__HTML__TOOLS": "html_outline",
        },
    )
    assert set(names) == {"html_outline"}

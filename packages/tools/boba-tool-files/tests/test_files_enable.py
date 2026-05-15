"""Тесты enable-конвенции и сборки FilesPlugin."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from boba.plugin import ExtensionContext, install_plugins
from boba.tool.files import FilesPlugin
from boba.workspace.contract import ProjectWorkspaceShell

_FILES_TOOL_NAMES = {
    "append", "cat", "cd", "cp", "edit", "grep", "ls", "mkdir",
    "mv", "pwd", "rm", "stat", "touch", "tree", "write",
}


def _ext_ctx() -> ExtensionContext:
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


def _tool_names(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> list[str]:
    monkeypatch.delenv("BOBA_TOOL__FILES__ENABLE", raising=False)
    monkeypatch.delenv("BOBA_TOOL__FILES__TOOLS", raising=False)
    monkeypatch.delenv("BOBA_CONFIG_PATH", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sources = list(install_plugins([FilesPlugin], _ext_ctx()))
    return [t.name() for src in sources for t in src.tools()]


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch):
    assert _tool_names(monkeypatch, {}) == []


def test_disabled_explicit(monkeypatch: pytest.MonkeyPatch):
    assert _tool_names(monkeypatch, {"BOBA_TOOL__FILES__ENABLE": "false"}) == []


def test_enabled_yields_all_tools(monkeypatch: pytest.MonkeyPatch):
    names = _tool_names(monkeypatch, {"BOBA_TOOL__FILES__ENABLE": "true"})
    assert set(names) == _FILES_TOOL_NAMES


def test_allowlist_filters_tools(monkeypatch: pytest.MonkeyPatch):
    names = _tool_names(
        monkeypatch,
        {
            "BOBA_TOOL__FILES__ENABLE": "true",
            "BOBA_TOOL__FILES__TOOLS": "cat,grep",
        },
    )
    assert set(names) == {"cat", "grep"}


def test_allowlist_unknown_name_raises(monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(ValueError, match="unknown names"):
        _tool_names(
            monkeypatch,
            {
                "BOBA_TOOL__FILES__ENABLE": "true",
                "BOBA_TOOL__FILES__TOOLS": "cat,nonsense",
            },
        )

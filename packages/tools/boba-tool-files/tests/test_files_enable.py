"""Тесты enable-конвенции и сборки FilesPlugin."""

from __future__ import annotations

from unittest.mock import MagicMock

from boba.config.bundle import ConfigBundle
from boba.config.source.dict import DictSource
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


def _tool_names(values: dict[str, str]) -> list[str]:
    bundle = ConfigBundle.from_sources([DictSource.from_strings(values)])
    sources = list(install_plugins(bundle, [FilesPlugin], _ext_ctx()))
    return [t.name().to_wire() for src in sources for t in src.tools()]


def test_disabled_by_default():
    assert _tool_names({}) == []


def test_disabled_explicit():
    assert _tool_names({"tool.files.enable": "false"}) == []


def test_enabled_yields_all_tools():
    names = _tool_names({"tool.files.enable": "true"})
    assert set(names) == _FILES_TOOL_NAMES


def test_allowlist_filters_tools():
    names = _tool_names(
        {"tool.files.enable": "true", "tool.files.tools": "cat,grep"},
    )
    assert set(names) == {"cat", "grep"}


def test_allowlist_unknown_name_raises():
    import pytest

    with pytest.raises(ValueError, match="unknown names"):
        _tool_names(
            {"tool.files.enable": "true", "tool.files.tools": "cat,nonsense"},
        )

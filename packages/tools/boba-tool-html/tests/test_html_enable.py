"""Тесты enable-конвенции и сборки HtmlPlugin."""

from __future__ import annotations

from unittest.mock import MagicMock

from boba.config.bundle import ConfigBundle
from boba.config.source.dict import DictSource
from boba.plugin import ExtensionContext, install_plugins
from boba.tool.html import HtmlPlugin
from boba.workspace.contract import ProjectWorkspaceShell


def _ext_ctx() -> ExtensionContext:
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


def _tool_names(values: dict[str, str]) -> list[str]:
    bundle = ConfigBundle.from_sources([DictSource.from_strings(values)])
    sources = list(install_plugins(bundle, [HtmlPlugin], _ext_ctx()))
    return [t.name() for src in sources for t in src.tools()]


def test_disabled_by_default():
    assert _tool_names({}) == []


def test_disabled_explicit():
    assert _tool_names({"tool.html.enable": "false"}) == []


def test_enabled_yields_both_tools():
    names = _tool_names({"tool.html.enable": "true"})
    assert set(names) == {"html_outline", "html_section"}


def test_allowlist_filters_tools():
    names = _tool_names(
        {"tool.html.enable": "true", "tool.html.tools": "html_outline"},
    )
    assert set(names) == {"html_outline"}

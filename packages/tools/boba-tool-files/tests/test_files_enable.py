"""Тесты enable-конвенции и сборки FilesPlugin."""

from __future__ import annotations

from unittest.mock import MagicMock

from boba.config.bundle import ConfigBundle
from boba.config.path import (
    ConfigLookup,
    ConfigPath,
    ConfigSource,
    Found,
    NotFound,
)
from boba.patterns import StrId
from boba.plugin import ExtensionContext, install_plugins
from boba.schema.value import StringValue
from boba.tool.files import FilesPlugin
from boba.workspace.contract import ProjectWorkspaceRegistry


class _InlineSource(ConfigSource):
    def __init__(self, vals: dict[str, str]) -> None:
        self._vals = {ConfigPath.parse(k): StringValue(v) for k, v in vals.items()}

    def name(self) -> str:
        return "inline"

    def priority(self) -> int:
        return 100

    def load(self):
        return dict(self._vals)

    def lookup(self, path: ConfigPath) -> ConfigLookup:
        if path in self._vals:
            return Found(self._vals[path])
        return NotFound()

    def keys_with_prefix(self, prefix: ConfigPath):
        for p in self._vals:
            if p.startswith(prefix):
                yield p

    @property
    def id(self) -> StrId:
        return StrId("inline")


_FILES_TOOL_NAMES = {
    "append", "cat", "cd", "cp", "edit", "grep", "ls", "mkdir",
    "mv", "pwd", "rm", "stat", "touch", "tree", "write",
}


def _ext_ctx() -> ExtensionContext:
    return ExtensionContext({
        ProjectWorkspaceRegistry: MagicMock(spec=ProjectWorkspaceRegistry),
    })


def _tool_names(values: dict[str, str]) -> list[str]:
    bundle = ConfigBundle.from_sources([_InlineSource(values)])
    sources = list(install_plugins(bundle, [FilesPlugin], _ext_ctx()))
    return [t.name().to_wire() for src in sources for t in src.tools()]


def test_disabled_by_default():
    assert _tool_names({}) == []


def test_disabled_explicit():
    assert _tool_names({"tool.files.enable": "false"}) == []


def test_enabled_yields_all_tools():
    names = _tool_names({"tool.files.enable": "true"})
    assert set(names) == _FILES_TOOL_NAMES

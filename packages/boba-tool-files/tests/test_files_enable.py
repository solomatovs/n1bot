"""Тесты механики [ext.files] enable / tools_allow."""

from __future__ import annotations

from boba.config.app import ConfigSectionFactory
from boba.config.bundle import ConfigBundle
from boba.config.path import (
    ConfigLookup,
    ConfigPath,
    ConfigSource,
    Found,
    NotFound,
)
from boba.ext.files_tools import register_tools as files_register_tools
from boba.ext.files_tools.append import AppendToolSection
from boba.ext.files_tools.cat import CatToolSection
from boba.ext.files_tools.cd import CdToolSection
from boba.ext.files_tools.config import FilesSection
from boba.ext.files_tools.cp import CpToolSection
from boba.ext.files_tools.edit import EditToolSection
from boba.ext.files_tools.grep import GrepToolSection
from boba.ext.files_tools.ls import LsToolSection
from boba.ext.files_tools.mkdir import MkdirToolSection
from boba.ext.files_tools.mv import MvToolSection
from boba.ext.files_tools.pwd import PwdToolSection
from boba.ext.files_tools.rm import RmToolSection
from boba.ext.files_tools.stat import StatToolSection
from boba.ext.files_tools.touch import TouchToolSection
from boba.ext.files_tools.tree import TreeToolSection
from boba.ext.files_tools.write import WriteToolSection
from boba.patterns import StrId
from boba.tools.framework import ExtensionContext
from boba.value import StringValue


_FILES_SECTIONS = (
    FilesSection(),
    AppendToolSection(),
    CatToolSection(),
    CdToolSection(),
    CpToolSection(),
    EditToolSection(),
    GrepToolSection(),
    LsToolSection(),
    MkdirToolSection(),
    MvToolSection(),
    PwdToolSection(),
    RmToolSection(),
    StatToolSection(),
    TouchToolSection(),
    TreeToolSection(),
    WriteToolSection(),
)


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


def _make_app(values: dict[str, str]):
    bundle = ConfigBundle.from_sources([_InlineSource(values)])
    factory = ConfigSectionFactory()
    # Регистрируем только files-секции явно, чтобы тест не зависел от того,
    # какие посторонние плагины установлены в окружении (их required-поля
    # упали бы при build из-за пустого bundle).
    for section in _FILES_SECTIONS:
        factory.register_section(section)
    return factory.build(bundle)


def _tool_names(app) -> list[str]:
    sources = list(files_register_tools(ExtensionContext(config=app)))
    return [t.tool_id().to_wire() for src in sources for t in src.tools()]


def test_disabled_by_default_when_section_absent():
    assert _tool_names(_make_app({})) == []


def test_explicit_disable():
    assert _tool_names(_make_app({"$ext.files.enable": "false"})) == []


def test_enabled_yields_all_tools():
    names = _tool_names(_make_app({"$ext.files.enable": "true"}))
    assert {"cat", "ls", "grep", "pwd", "edit", "write"} <= set(names)


def test_tools_allow_filters_subset():
    app = _make_app(
        {"$ext.files.enable": "true", "$ext.files.tools_allow": "cat,ls,grep"}
    )
    assert set(_tool_names(app)) == {"cat", "ls", "grep"}


def test_tools_allow_empty_means_all():
    app = _make_app({"$ext.files.enable": "true", "$ext.files.tools_allow": ""})
    assert len(_tool_names(app)) > 5

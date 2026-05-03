"""Тесты механики [ext.files] enable / tools_allow."""

from __future__ import annotations

from boba.config.app import AppConfigFactory
from boba.config.bundle import ConfigBundle
from boba.config.path import (
    ConfigLookup,
    ConfigPath,
    ConfigSource,
    Found,
    NotFound,
)
from boba.ext.files import register_tools as files_register_tools
from boba.patterns import StrId
from boba.tools import ExtensionContext
from boba.value import StringValue


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
    factory = AppConfigFactory()
    factory.discover_extension_sections()
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
    app = _make_app(
        {"$ext.files.enable": "true", "$ext.files.tools_allow": ""}
    )
    assert len(_tool_names(app)) > 5

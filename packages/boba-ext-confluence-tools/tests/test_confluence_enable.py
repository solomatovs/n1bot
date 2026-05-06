"""Тесты механики [ext.confluence] enable / tools_allow."""

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
from boba.ext.confluence_tools import register_tools as confluence_register_tools
from boba.patterns import StrId
from boba.tools.framework import ExtensionContext
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
    # ConfluenceSearchSection требует base_url/auth_token; auto-discovery
    # материализует все registered-секции, поэтому фоном задаём минимум.
    base_values = {
        "$ext.confluence.search.base_url": "https://example.test",
        "$ext.confluence.search.auth_token": "test-token",
    }
    bundle = ConfigBundle.from_sources([_InlineSource({**base_values, **values})])
    factory = ConfigSectionFactory()
    factory.discover_extension_sections()
    return factory.build(bundle)


def _tool_names(app) -> list[str]:
    sources = list(confluence_register_tools(ExtensionContext(config=app)))
    return [t.tool_id().to_wire() for src in sources for t in src.tools()]


def test_disabled_by_default():
    assert _tool_names(_make_app({})) == []


def test_enabled_yields_all_tools():
    names = _tool_names(_make_app({"$ext.confluence.enable": "true"}))
    assert set(names) == {
        "confluence_outline",
        "confluence_search",
        "confluence_section",
    }

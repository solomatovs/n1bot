"""Тесты механики per-ext enable / tools_allow.

Каждый ext-пакет регистрирует свою ConfigSection (`enable: bool = False`,
`tools_allow: list[str] = []`); register_tools(ctx) гейтит вывод по cfg.enable
и фильтрует по cfg.tools_allow (пустой список = все).
"""

from __future__ import annotations

from boba.config.app import AppConfigFactory
from boba.config.bundle import ConfigBundle
from boba.ext.confluence import register_tools as confluence_register_tools
from boba.ext.confluence.config import ConfluenceSection
from boba.ext.files import register_tools as files_register_tools
from boba.ext.files.config import FilesSection
from boba.ext.html import register_tools as html_register_tools
from boba.ext.html.config import HtmlSection
from boba.tools import ExtensionContext


def _make_app(values: dict[str, str | bool | list[str]]) -> object:
    """Собрать AppConfig с inline-ConfigSource поверх дефолтов."""
    from boba.config.path import (
        ConfigLookup,
        ConfigPath,
        ConfigSource,
        Found,
        NotFound,
    )
    from boba.patterns import StrId
    from boba.value import StringValue

    class InlineSource(ConfigSource):
        def __init__(self, vals: dict[str, str | bool | list[str]]) -> None:
            self._vals = {ConfigPath.parse(k): v for k, v in vals.items()}

        def name(self) -> str:
            return "inline"

        def priority(self) -> int:
            return 100

        def load(self) -> dict[ConfigPath, object]:
            return {p: StringValue(str(v)) for p, v in self._vals.items()}

        def lookup(self, path: ConfigPath) -> ConfigLookup:
            if path in self._vals:
                return Found(StringValue(str(self._vals[path])))
            return NotFound()

        def keys_with_prefix(self, prefix: ConfigPath):
            for p in self._vals:
                if p.startswith(prefix):
                    yield p

        @property
        def id(self) -> StrId:
            return StrId("inline")

    bundle = ConfigBundle.from_sources([InlineSource(values)])
    factory = AppConfigFactory()
    factory.register_section(FilesSection())
    factory.register_section(HtmlSection())
    factory.register_section(ConfluenceSection())
    return factory.build(bundle)


def _tool_names(register_fn, app) -> list[str]:
    """Запустить register_tools и собрать имена tool'ов."""
    sources = list(register_fn(ExtensionContext(config=app)))
    names: list[str] = []
    for src in sources:
        for tool in src.tools():
            names.append(tool.tool_id().to_wire())
    return names


# ── enable=false (default) ──────────────────────────────────────────


def test_files_disabled_by_default_when_section_absent():
    app = _make_app({})
    assert _tool_names(files_register_tools, app) == []


def test_html_disabled_by_default():
    app = _make_app({})
    assert _tool_names(html_register_tools, app) == []


def test_confluence_disabled_by_default():
    app = _make_app({})
    assert _tool_names(confluence_register_tools, app) == []


def test_files_explicit_disable():
    app = _make_app({"$ext.files.enable": "false"})
    assert _tool_names(files_register_tools, app) == []


# ── enable=true: все tools ──────────────────────────────────────────


def test_files_enabled_yields_all_tools():
    app = _make_app({"$ext.files.enable": "true"})
    names = _tool_names(files_register_tools, app)
    assert {"cat", "ls", "grep", "pwd", "edit", "write"} <= set(names)


def test_html_enabled_yields_both_tools():
    app = _make_app({"$ext.html.enable": "true"})
    assert set(_tool_names(html_register_tools, app)) == {
        "html_outline",
        "html_section",
    }


def test_confluence_enabled_yields_both_tools():
    app = _make_app({"$ext.confluence.enable": "true"})
    assert set(_tool_names(confluence_register_tools, app)) == {
        "confluence_outline",
        "confluence_section",
    }


# ── tools_allow per-ext ─────────────────────────────────────────────


def test_files_tools_allow_filters_subset():
    app = _make_app(
        {
            "$ext.files.enable": "true",
            "$ext.files.tools_allow": "cat,ls,grep",
        }
    )
    assert set(_tool_names(files_register_tools, app)) == {"cat", "ls", "grep"}


def test_html_tools_allow_single():
    app = _make_app(
        {"$ext.html.enable": "true", "$ext.html.tools_allow": "html_outline"}
    )
    assert _tool_names(html_register_tools, app) == ["html_outline"]


def test_files_tools_allow_empty_means_all():
    app = _make_app({"$ext.files.enable": "true", "$ext.files.tools_allow": ""})
    names = _tool_names(files_register_tools, app)
    assert len(names) > 5

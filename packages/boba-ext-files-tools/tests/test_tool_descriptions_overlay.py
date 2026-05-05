"""Тест end-to-end overlay-описаний tools через ConfigSection.

Operator задаёт `[ext.files.tools.cat] description = "..."` и
`[ext.files.tools.cat.params.path] description = "..."`. Эти значения
доходят до `CatTool.definition()` через стандартный механизм
`ctx.config.section(CatToolSection)`.
"""

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
from boba.ext.files_tools.cat import CatTool, CatToolConfig
from boba.patterns import StrId
from boba.tools.domain import ParamOverlay
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
    bundle = ConfigBundle.from_sources([_InlineSource(values)])
    factory = ConfigSectionFactory()
    factory.discover_extension_sections()
    return factory.build(bundle)


def _cat_tool(app):
    sources = list(files_register_tools(ExtensionContext(config=app)))
    return next(
        t for src in sources for t in src.tools() if t.tool_id().to_wire() == "cat"
    )


def test_tool_description_default_when_no_overlay():
    app = _make_app({"$ext.files.enable": "true"})
    cat = _cat_tool(app)
    schema = cat.definition()
    # Дефолт из CatTool.DEFAULT_DESCRIPTION — обычная строка.
    assert isinstance(schema.description, str)
    assert schema.description == CatTool.DEFAULT_DESCRIPTION


def test_tool_description_overridden_via_toml():
    app = _make_app(
        {
            "$ext.files.enable": "true",
            "$ext.files.tools.cat.description": "OPERATOR override.",
        }
    )
    cat = _cat_tool(app)
    schema = cat.definition()
    assert schema.description == "OPERATOR override."


def test_param_description_overridden_via_toml():
    app = _make_app(
        {
            "$ext.files.enable": "true",
            "$ext.files.tools.cat.params.path.description": "OPERATOR path.",
        }
    )
    cat = _cat_tool(app)
    schema = cat.definition()
    path_field = next(f for f in schema.fields if f.name == "path")
    assert path_field.description == "OPERATOR path."
    # Другие параметры — дефолтные.
    encoding_field = next(f for f in schema.fields if f.name == "encoding")
    assert encoding_field.description == CatTool.DEFAULT_ENCODING_DESC


def test_runtime_setting_overridden_via_toml():
    """max_lines runtime-параметр меняется через [ext.files.tools.cat] max_lines=N."""
    app = _make_app(
        {
            "$ext.files.enable": "true",
            "$ext.files.tools.cat.max_lines": "555",
        }
    )
    cat = _cat_tool(app)
    cfg: CatToolConfig = cat._cfg  # type: ignore[attr-defined]
    assert cfg.max_lines == 555


def test_cat_tool_can_be_instantiated_directly():
    """Sanity: CatTool(CatToolConfig(...)) работает без AppConfig (для unit-тестов)."""
    cfg = CatToolConfig(
        description="test",
        max_lines=100,
        params={"path": ParamOverlay(description="test path")},
    )
    cat = CatTool(cfg)
    schema = cat.definition()
    assert schema.description == "test"
    assert isinstance(schema.description, str)
    path_field = next(f for f in schema.fields if f.name == "path")
    assert path_field.description == "test path"

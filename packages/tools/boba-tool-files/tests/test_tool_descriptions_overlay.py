"""Тесты overlay-описаний tools через FilesPlugin (новая Plugin-модель).

Operator задаёт `[tool.files.cat.prompt] description = "..."` и
`[tool.files.cat.prompt.fields.path] = "..."`. Эти значения доходят до
`CatTool.definition()` через `cfg.prompt.apply(...)`.
"""

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
from boba.plugin.prompt import PromptOverlay
from boba.schema.value import StringValue
from boba.tool.files import FilesPlugin
from boba.tool.files.cat import CatTool, CatToolConfig
from boba.tools.domain import ToolSourceId
from boba.workspace.contract import ProjectWorkspaceShell


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


_BASE = {"tool.files.enable": "true"}


def _ext_ctx() -> ExtensionContext:
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


def _cat_tool(values: dict[str, str]) -> CatTool:
    bundle = ConfigBundle.from_sources([_InlineSource({**_BASE, **values})])
    sources = list(install_plugins(bundle, [FilesPlugin], _ext_ctx()))
    found = next(
        t for src in sources for t in src.tools() if t.name().to_wire() == "cat"
    )
    assert isinstance(found, CatTool)
    return found


def test_tool_description_default_when_no_overlay():
    cat = _cat_tool({})
    schema = cat.definition()
    assert "Прочитать строки" in schema.description


def test_tool_description_overridden_via_toml():
    cat = _cat_tool({"tool.files.cat.prompt.description": "OPERATOR override."})
    schema = cat.definition()
    assert schema.description == "OPERATOR override."


def test_param_description_overridden_via_toml():
    cat = _cat_tool(
        {"tool.files.cat.prompt.fields.path": "OPERATOR path."},
    )
    schema = cat.definition()
    path_field = next(f for f in schema.fields if f.name == "path")
    assert path_field.description == "OPERATOR path."
    # Другие параметры — дефолтные.
    encoding_field = next(f for f in schema.fields if f.name == "encoding")
    assert "utf-8" in encoding_field.description


def test_runtime_setting_overridden_via_toml():
    """max_lines runtime-параметр меняется через `[tool.files.cat] max_lines=N`."""
    cat = _cat_tool({"tool.files.cat.max_lines": "555"})
    cfg: CatToolConfig = cat._cfg  # type: ignore[attr-defined]
    assert cfg.max_lines == 555


def test_cat_tool_can_be_instantiated_directly():
    """Sanity: CatTool(CatToolConfig(...), ctx) работает без AppConfig."""
    cfg = CatToolConfig(
        max_lines=100,
        prompt=PromptOverlay(
            description="test",
            fields={"path": "test path"},
        ),
    )
    cat = CatTool(cfg, _ext_ctx(), ToolSourceId("test"))
    schema = cat.definition()
    assert schema.description == "test"
    path_field = next(f for f in schema.fields if f.name == "path")
    assert path_field.description == "test path"

"""Тесты overlay-описаний tools через FilesPlugin (новая Plugin-модель).

Operator задаёт `[tool.files.cat.prompt] description = "..."` и
`[tool.files.cat.prompt.fields.path] = "..."`. Эти значения доходят до
`CatTool.definition()` через `cfg.prompt.apply(...)`.
"""

from __future__ import annotations

from collections.abc import Callable

from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tool.files.cat import CatTool, CatToolConfig
from boba.tools.domain import ToolSourceId


def test_tool_description_default_when_no_overlay(
    make_cat_tool: Callable[..., CatTool],
):
    cat = make_cat_tool()
    schema = cat.definition()
    assert "Прочитать строки" in schema.description


def test_tool_description_overridden_via_toml(
    make_cat_tool: Callable[..., CatTool],
):
    cat = make_cat_tool(
        '\n[tool.files.cat.prompt]\ndescription = "OPERATOR override."\n',
    )
    schema = cat.definition()
    assert schema.description == "OPERATOR override."


def test_param_description_overridden_via_toml(
    make_cat_tool: Callable[..., CatTool],
):
    cat = make_cat_tool(
        '\n[tool.files.cat.prompt.fields]\npath = "OPERATOR path."\n',
    )
    schema = cat.definition()
    props = schema.parameters_schema["properties"]
    assert props["path"]["description"] == "OPERATOR path."
    assert "utf-8" in props["encoding"]["description"]


def test_runtime_setting_overridden_via_toml(
    make_cat_tool: Callable[..., CatTool],
):
    """max_lines runtime-параметр меняется через `[tool.files.cat] max_lines=N`."""
    cat = make_cat_tool("\n[tool.files.cat]\nmax_lines = 555\n")
    cfg: CatToolConfig = cat._cfg  # type: ignore[attr-defined]
    assert cfg.max_lines == 555


def test_cat_tool_can_be_instantiated_directly(
    ext_ctx: ExtensionContext,
):
    """Sanity: CatTool(CatToolConfig(...), ctx) работает без AppConfig."""
    cfg = CatToolConfig(
        max_lines=100,
        prompt=PromptOverlay(
            description="test",
            fields={"path": "test path"},
        ),
    )
    cat = CatTool(cfg, ext_ctx, ToolSourceId("test"))
    schema = cat.definition()
    assert schema.description == "test"
    assert schema.parameters_schema["properties"]["path"]["description"] == "test path"

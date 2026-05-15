"""Тесты overlay-описаний tools через FilesPlugin (новая Plugin-модель).

Operator задаёт `[tool.files.cat.prompt] description = "..."` и
`[tool.files.cat.prompt.fields.path] = "..."`. Эти значения доходят до
`CatTool.definition()` через `cfg.prompt.apply(...)`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boba.plugin import ExtensionContext, install_plugins
from boba.plugin.prompt import PromptOverlay
from boba.tool.files import FilesPlugin
from boba.tool.files.cat import CatTool, CatToolConfig
from boba.tools.domain import ToolSourceId
from boba.workspace.contract import ProjectWorkspaceShell


def _ext_ctx() -> ExtensionContext:
    return ExtensionContext({
        ProjectWorkspaceShell: MagicMock(spec=ProjectWorkspaceShell),
    })


def _cat_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    toml_body: str = "",
) -> CatTool:
    """TOML c включённым FilesPlugin + опциональным overlay-блоком."""
    cfg_path = tmp_path / "boba.toml"
    cfg_path.write_text("[tool.files]\nenable = true\n" + toml_body, encoding="utf-8")
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(cfg_path))
    monkeypatch.delenv("BOBA_TOOL__FILES__ENABLE", raising=False)
    monkeypatch.delenv("BOBA_TOOL__FILES__TOOLS", raising=False)
    sources = list(install_plugins([FilesPlugin], _ext_ctx()))
    found = next(t for src in sources for t in src.tools() if t.name() == "cat")
    assert isinstance(found, CatTool)
    return found


def test_tool_description_default_when_no_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    cat = _cat_tool(monkeypatch, tmp_path)
    schema = cat.definition()
    assert "Прочитать строки" in schema["description"]


def test_tool_description_overridden_via_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    cat = _cat_tool(
        monkeypatch,
        tmp_path,
        '\n[tool.files.cat.prompt]\ndescription = "OPERATOR override."\n',
    )
    schema = cat.definition()
    assert schema["description"] == "OPERATOR override."


def test_param_description_overridden_via_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    cat = _cat_tool(
        monkeypatch,
        tmp_path,
        '\n[tool.files.cat.prompt.fields]\npath = "OPERATOR path."\n',
    )
    schema = cat.definition()
    props = schema["properties"]
    assert props["path"]["description"] == "OPERATOR path."
    assert "utf-8" in props["encoding"]["description"]


def test_runtime_setting_overridden_via_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """max_lines runtime-параметр меняется через `[tool.files.cat] max_lines=N`."""
    cat = _cat_tool(
        monkeypatch,
        tmp_path,
        "\n[tool.files.cat]\nmax_lines = 555\n",
    )
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
    assert schema["description"] == "test"
    assert schema["properties"]["path"]["description"] == "test path"

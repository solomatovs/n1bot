"""Integration: confluence_page_section через полный путь LLM tool-call."""

from __future__ import annotations

import json

import pytest

from boba.config.app import AppConfig
from boba.ext.confluence_runtime.tools.page import ConfluencePageSection
from boba.ext.confluence_runtime.tools.page_section import (
    ConfluencePageSectionTool,
    ConfluencePageSectionToolSection,
)
from boba.tools.domain import JsonResult, ToolContext

pytestmark = pytest.mark.integration


def test_page_section_via_tool_call(
    real_app: AppConfig, live_page_id: str, live_anchor: str,
) -> None:
    """JSON-аргументы → schema-валидация → реальный REST → JsonResult."""
    tool = ConfluencePageSectionTool(
        tool_cfg=real_app.section(ConfluencePageSectionToolSection),
        runtime_cfg=real_app.section(ConfluencePageSection),
    )
    ctx = ToolContext(project_workspace=None)  # type: ignore[arg-type]
    arguments = json.loads(
        json.dumps({
            "page_id": live_page_id,
            "anchor": live_anchor,
            "max_chars": 2000,
        }),
    )
    typed = tool.args_converter().convert(arguments)
    result = tool.execute(ctx, typed)
    assert isinstance(result, JsonResult)

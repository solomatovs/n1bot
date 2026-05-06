"""Integration: confluence_page_outline через полный путь LLM tool-call."""

from __future__ import annotations

import json

import pytest

from boba.config.app import AppConfig
from boba.ext.confluence_runtime.tools.page import ConfluencePageSection
from boba.ext.confluence_runtime.tools.page_outline import (
    ConfluencePageOutlineTool,
    ConfluencePageOutlineToolSection,
)
from boba.tools.domain import JsonResult, ToolContext

pytestmark = pytest.mark.integration


def test_page_outline_via_tool_call(
    real_app: AppConfig, live_page_id: str,
) -> None:
    """JSON-аргументы → schema-валидация → реальный REST → JsonResult."""
    tool = ConfluencePageOutlineTool(
        tool_cfg=real_app.section(ConfluencePageOutlineToolSection),
        runtime_cfg=real_app.section(ConfluencePageSection),
    )
    ctx = ToolContext(project_workspace=None)  # type: ignore[arg-type]
    arguments = json.loads(
        json.dumps({"page_id": live_page_id, "max_headings": 50}),
    )
    typed = tool.args_converter().convert(arguments)
    result = tool.execute(ctx, typed)
    assert isinstance(result, JsonResult)

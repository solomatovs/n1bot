"""Integration: confluence_search через полный путь LLM tool-call."""

from __future__ import annotations

import json

import pytest

from boba.config.app import AppConfig
from boba.ext.confluence_runtime.tools.search import (
    ConfluenceSearchSection,
    ConfluenceSearchTool,
    ConfluenceSearchToolSection,
)
from boba.tools.domain import JsonResult, ToolContext

pytestmark = pytest.mark.integration


def test_search_via_tool_call(real_app: AppConfig) -> None:
    """JSON-аргументы → schema-валидация → реальный REST → JsonResult."""
    tool = ConfluenceSearchTool(
        tool_cfg=real_app.section(ConfluenceSearchToolSection),
        runtime_cfg=real_app.section(ConfluenceSearchSection),
    )
    ctx = ToolContext(project_workspace=None)  # type: ignore[arg-type]
    arguments = json.loads('{"query": "page", "limit": 5}')
    typed = tool.args_converter().convert(arguments)
    result = tool.execute(ctx, typed)
    assert isinstance(result, JsonResult)

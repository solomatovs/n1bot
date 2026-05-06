"""
Эмуляция полного пути LLM tool-call → confluence_search.
"""

from __future__ import annotations

import json

import pytest

from boba.config.app import AppConfig
from boba.ext.confluence_tools.search import (
    ConfluenceSearchSection,
    ConfluenceSearchTool,
    ConfluenceSearchToolSection,
)
from boba.tools.domain import (
    JsonResult,
    ToolContext,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def tool(real_app: AppConfig) -> ConfluenceSearchTool:
    return ConfluenceSearchTool(
        tool_cfg=real_app.section(ConfluenceSearchToolSection),
        runtime_cfg=real_app.section(ConfluenceSearchSection),
    )


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(project_workspace=None)  # type: ignore[arg-type]


def _llm_call(tool: ConfluenceSearchTool, ctx: ToolContext, args_json: str):
    """Полный путь: JSON-строка → dict → schema.convert → tool.execute."""
    arguments = json.loads(args_json)
    typed_args = tool.args_converter().convert(arguments)
    return tool.execute(ctx, typed_args)


def test_valid_tool_call_returns_json_result(
    tool: ConfluenceSearchTool, ctx: ToolContext,
) -> None:
    """Корректный tool-call от LLM → реальный поиск → JsonResult."""
    result = _llm_call(tool, ctx, '{"query": "adqm", "limit": 5}')
    assert isinstance(result, JsonResult)
    payload = result.payload
    assert payload["cql"].startswith("text ~ ")
    assert isinstance(payload["hits"], list)
    assert len(payload["hits"]) <= 5


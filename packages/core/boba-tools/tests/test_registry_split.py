"""Smoke-тесты разделения ToolRegistry / ToolCatalog / ToolExecutor.

Tool конструируется как `DishkaTool` поверх минимального Dishka-контейнера
— естественный путь после v2 миграции.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from dishka import make_container

from boba.tools import tool
from boba.tools.adapter import DishkaTool
from boba.tools.domain import (
    TextResult,
    ToolContext,
    ToolId,
    ToolName,
    ToolResult,
    ToolSourceId,
    compose_tool_id,
)
from boba.tools.framework import (
    StaticToolSource,
    ToolCatalog,
    ToolExecutor,
    ToolRegistry,
)
from boba.tools.introspect import introspect_callable


@tool
class _EchoTool:
    """Echo tool: вернуть переданный текст как TextResult."""

    def __call__(self, text: str) -> ToolResult:
        return TextResult(text=text)


def _make_tool(source: ToolSourceId, name: str = "echo") -> DishkaTool:
    plan = introspect_callable(_EchoTool)
    # Подменяем имя в плане, чтобы тест мог собрать несколько tool'ов под разные имена.
    plan = replace(plan, name=name)
    container = make_container()
    return DishkaTool(
        target=_EchoTool(),
        plan=plan,
        container=container,
        component="",
        source_id=source,
    )


def _registry_with_one(source_id: str = "src") -> ToolRegistry:
    src = ToolSourceId(source_id)
    return ToolRegistry.from_sources(
        [StaticToolSource(src, [_make_tool(src, "echo")])],
    )


def test_catalog_returns_definitions_for_all_sources():
    registry = _registry_with_one()
    catalog = registry.catalog()

    schemas = list(catalog.definitions())
    assert len(schemas) == 1
    assert schemas[0].name == compose_tool_id(ToolSourceId("src"), ToolName("echo"))


def test_catalog_and_executor_share_state():
    """Catalog видит ровно те tool'ы, которые сможет вызвать executor."""
    registry = _registry_with_one()
    catalog_ids = {s.name for s in registry.catalog().definitions()}
    expected = compose_tool_id(ToolSourceId("src"), ToolName("echo"))

    assert expected in catalog_ids

    from boba.tools.domain import ToolCall, ToolStreamCompleted

    events = list(
        registry.executor().stream(
            ToolContext(),
            ToolCall(tool_id=expected, arguments={"text": "hi"}),
        ),
    )
    # Plain-function tool: один `ToolStreamCompleted` с результатом.
    assert len(events) == 1
    assert isinstance(events[0], ToolStreamCompleted)
    assert isinstance(events[0].result, TextResult)
    assert events[0].result.text == "hi"


def test_executor_unknown_tool_lists_available():
    registry = _registry_with_one()
    executor = registry.executor()

    from boba.tools.domain import ToolCall, ToolExecutionError

    with pytest.raises(ToolExecutionError) as ei:
        list(
            executor.stream(
                ToolContext(),
                ToolCall(tool_id=ToolId("src__missing"), arguments={}),
            ),
        )
    assert "not found" in str(ei.value)
    assert "src__echo" in str(ei.value)


def test_registry_close_caskades_to_sources():
    closed: list[ToolSourceId] = []

    class _RecordingSource(StaticToolSource):
        def close(self) -> None:
            closed.append(self.id())

    src = ToolSourceId("rec")
    registry = ToolRegistry.from_sources(
        [_RecordingSource(src, [_make_tool(src, "echo")])],
    )
    registry.close()
    assert closed == [src]


def test_catalog_is_lightweight_view():
    registry = _registry_with_one()
    a = registry.catalog()
    b = registry.catalog()
    assert isinstance(a, ToolCatalog)
    assert isinstance(b, ToolCatalog)
    assert [s.name for s in a.definitions()] == [s.name for s in b.definitions()]


def test_executor_is_lightweight_view():
    registry = _registry_with_one()
    assert isinstance(registry.executor(), ToolExecutor)
    # Второй вызов отдаёт новый объект — лёгкая proxy-семантика.
    assert registry.executor() is not registry.executor()

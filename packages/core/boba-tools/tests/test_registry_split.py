"""Smoke-тесты разделения ToolRegistry / ToolCatalog / ToolExecutor."""

from __future__ import annotations

import pytest

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
    tool_factory,
)


def _make_tool(source: ToolSourceId, name: str):
    def _impl(text: str) -> ToolResult:
        return TextResult(text=text)

    factory = tool_factory(_impl, name=name)
    return factory.build(source)


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
    # ToolSchema.name — wire-id
    assert schemas[0].name == compose_tool_id(ToolSourceId("src"), ToolName("echo"))


def test_catalog_and_executor_share_state():
    """Catalog видит ровно те tool'ы, которые сможет вызвать executor."""
    registry = _registry_with_one()
    catalog_ids = {s.name for s in registry.catalog().definitions()}
    expected = compose_tool_id(ToolSourceId("src"), ToolName("echo"))

    assert expected in catalog_ids
    # executor найдёт тот же id (не упадёт с unknown_tool):
    from boba.tools.domain import ToolCall

    result = registry.executor().execute(
        ToolContext(),
        ToolCall(tool_id=expected, arguments={"text": "hi"}),
    )
    assert isinstance(result, TextResult)
    assert result.text == "hi"


def test_executor_unknown_tool_lists_available():
    registry = _registry_with_one()
    executor = registry.executor()

    from boba.tools.domain import ToolCall, ToolExecutionError

    with pytest.raises(ToolExecutionError) as ei:
        executor.execute(
            ToolContext(),
            ToolCall(tool_id=ToolId("src__missing"), arguments={}),
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
    # разные инстансы, но видят одинаковые definitions
    assert isinstance(a, ToolCatalog)
    assert isinstance(b, ToolCatalog)
    assert [s.name for s in a.definitions()] == [s.name for s in b.definitions()]


def test_executor_is_lightweight_view():
    registry = _registry_with_one()
    assert isinstance(registry.executor(), ToolExecutor)
    # Второй вызов отдаёт новый объект — лёгкая proxy-семантика.
    assert registry.executor() is not registry.executor()

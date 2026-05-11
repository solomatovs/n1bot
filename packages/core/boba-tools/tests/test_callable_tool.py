"""
Тесты декоратор `@tool` — для функций
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, cast
from unittest.mock import MagicMock

import pytest

from boba.schema import schema_from_dataclass
from boba.schema.coercion import MinValue
from boba.tools.domain import (
    JsonResult,
    TextResult,
    ToolContext,
    ToolId,
    ToolName,
    ToolResult,
    ToolSourceId,
)
from boba.tools.framework import ToolDecoratorFactory, tool_factory
from boba.workspace.contract import ProjectWorkspaceShell

_SOURCE = ToolSourceId("test")


def _ctx() -> ToolContext:
    return ToolContext(
        project_workspace=cast(ProjectWorkspaceShell, MagicMock()),
    )


#  имя и описание


def test_callable_instance_uses_class_name():
    class SearchTool:
        """Поиск по индексу."""

        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(SearchTool())
    assert factory.name == ToolName("SearchTool")
    assert factory.description == "Поиск по индексу."
    assert factory.schema.description == "Поиск по индексу."


def test_name_override_replaces_class_name():
    class SearchTool:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(SearchTool(), name="search")
    assert factory.name == ToolName("search")


def test_description_override_replaces_docstring():
    class SearchTool:
        """Doc."""

        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(SearchTool(), description="Custom desc.")
    assert factory.description == "Custom desc."


def test_no_docstring_yields_empty_description():
    class T:
        def __call__(self, q: str) -> ToolResult:
            return TextResult(text=q)

    assert tool_factory(T()).description == ""


# schema из __call__


def test_schema_extracted_from_call_signature():
    class T:
        def __call__(
            self,
            query: Annotated[str, "Поисковая строка."],
            limit: Annotated[int, "Лимит.", MinValue(1)] = 10,
        ) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(T())
    fields = {f.name: f for f in factory.schema.fields}
    assert set(fields) == {"query", "limit"}
    assert fields["query"].description == "Поисковая строка."
    assert fields["limit"].description == "Лимит."


def test_self_param_not_in_schema():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(T())
    assert [f.name for f in factory.schema.fields] == ["query"]


def test_tool_context_param_excluded_from_schema():
    class T:
        def __call__(self, ctx: ToolContext, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(T())
    assert [f.name for f in factory.schema.fields] == ["query"]
    assert factory.injects_ctx is True


# execute


def test_invoke_calls_instance_with_kwargs():
    class T:
        def __call__(self, query: str, limit: int = 5) -> ToolResult:
            return TextResult(text=f"{query}*{limit}")

    built = tool_factory(T()).build(_SOURCE)
    out = built.invoke(_ctx(), {"query": "x", "limit": 3})
    assert isinstance(out, TextResult)
    assert out.text == "x*3"


def test_invoke_injects_tool_context():
    received: list[ToolContext] = []

    class T:
        def __call__(self, ctx: ToolContext, query: str) -> ToolResult:
            received.append(ctx)
            return TextResult(text=query)

    built = tool_factory(T()).build(_SOURCE)
    ctx = _ctx()
    built.invoke(ctx, {"query": "x"})
    assert received == [ctx]


def test_instance_state_is_preserved_between_calls():
    class Counter:
        """Считает вызовы — стейт живёт в инстансе."""

        def __init__(self) -> None:
            self._n = 0

        def __call__(self, label: str) -> ToolResult:
            self._n += 1
            return TextResult(text=f"{label}:{self._n}")

    built = tool_factory(Counter()).build(_SOURCE)
    out1 = built.invoke(_ctx(), {"label": "a"})
    out2 = built.invoke(_ctx(), {"label": "b"})
    assert cast(TextResult, out1).text == "a:1"
    assert cast(TextResult, out2).text == "b:2"


# совмещение с schema_from_dataclass


def test_dataclass_tool_carries_both_config_schema_and_call_schema():
    """Tool — это dataclass с конфигом в полях и аргументами вызова в `__call__`.

    `schema_from_dataclass(cls)` даёт схему конфига этого tool'а.
    `tool_factory(instance)` даёт схему аргументов вызова.
    """

    @dataclass(frozen=True)
    class SearchTool:
        """Поиск по индексу."""

        index_path: Annotated[str, "Путь к индексу."]
        top_k: Annotated[int, "Размер выдачи.", MinValue(1)] = 5

        def __call__(self, query: str) -> ToolResult:
            return JsonResult(
                payload={
                    "index": self.index_path,
                    "k": self.top_k,
                    "query": query,
                },
            )

    config_schema = schema_from_dataclass(SearchTool)
    assert config_schema.factory is SearchTool
    assert {f.name for f in config_schema.fields} == {"index_path", "top_k"}

    instance = SearchTool(index_path="/srv/idx", top_k=3)
    factory = tool_factory(instance)
    assert factory.name == ToolName("SearchTool")
    assert {f.name for f in factory.schema.fields} == {"query"}

    out = factory.build(_SOURCE).invoke(_ctx(), {"query": "hello"})
    assert isinstance(out, JsonResult)
    assert out.payload == {"index": "/srv/idx", "k": 3, "query": "hello"}


# into_source / build


def test_into_source_yields_single_tool_source():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    src = tool_factory(T()).into_source(_SOURCE)
    assert src.id() == _SOURCE
    tools = list(src.tools())
    assert len(tools) == 1
    assert tools[0].tool_id() == ToolId.compose(_SOURCE, ToolName("T"))


def test_build_with_name_override():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    built = tool_factory(T(), name="renamed").build(_SOURCE)
    assert built.tool_id() == ToolId.compose(_SOURCE, ToolName("renamed"))


# ── ошибки ────────────────────────────────────────────────────────────────


def test_class_itself_rejected():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    with pytest.raises(TypeError, match="ожидается функция или callable"):
        tool_factory(T)  # type: ignore[arg-type]  # передаём класс, а не инстанс


def test_non_callable_rejected():
    with pytest.raises(TypeError, match="ожидается функция или callable"):
        tool_factory(42)  # type: ignore[arg-type]


def test_factory_from_callable_alias_matches_function_form():
    """ToolDecoratorFactory.from_callable работает и для функции, и для инстанса."""

    def fn(query: str) -> ToolResult:
        return TextResult(text=query)

    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    fn_factory = ToolDecoratorFactory.from_callable(fn)
    inst_factory = ToolDecoratorFactory.from_callable(T())
    assert fn_factory.name == ToolName("fn")
    assert inst_factory.name == ToolName("T")

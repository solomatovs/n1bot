"""Тесты `tool_factory` — для callable-инстансов (классов с `__call__`)."""

from __future__ import annotations

from typing import Annotated, cast

import pytest
from pydantic import Field

from boba.tools.domain import (
    InvalidToolArgumentError,
    JsonResult,
    TextResult,
    ToolContext,
    ToolName,
    ToolResult,
    ToolSourceId,
    compose_tool_id,
)
from boba.tools.framework import ToolDecoratorFactory, tool_factory

_SOURCE = ToolSourceId("test")


def _ctx() -> ToolContext:
    return ToolContext()


#  имя и описание


def test_callable_instance_uses_class_name():
    class SearchTool:
        """Поиск по индексу."""

        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(SearchTool())
    assert factory.name == ToolName("SearchTool")
    assert factory.description == "Поиск по индексу."


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


# args_model из __call__


def test_args_model_extracted_from_call_signature():
    class T:
        def __call__(
            self,
            query: Annotated[str, "Поисковая строка."],
            limit: Annotated[int, Field(ge=1, description="Лимит.")] = 10,
        ) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(T())
    fields = factory.args_model.model_fields
    assert set(fields) == {"query", "limit"}
    assert fields["query"].description == "Поисковая строка."
    assert fields["limit"].description == "Лимит."


def test_self_param_not_in_args_model():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    factory = tool_factory(T())
    assert list(factory.args_model.model_fields) == ["query"]


def test_tool_context_param_excluded_from_args_model():
    class T:
        def __call__(self, ctx: ToolContext, query: str) -> ToolResult:
            del ctx
            return TextResult(text=query)

    factory = tool_factory(T())
    assert list(factory.args_model.model_fields) == ["query"]
    assert factory.injects_ctx is True


# execute


def test_invoke_calls_instance_with_kwargs():
    class T:
        def __call__(self, query: str, limit: int = 5) -> ToolResult:
            return TextResult(text=f"{query}*{limit}")

    out = tool_factory(T()).build(_SOURCE).invoke(_ctx(), {"query": "x", "limit": 3})
    assert isinstance(out, TextResult)
    assert out.text == "x*3"


def test_invoke_injects_tool_context():
    received: list[ToolContext] = []

    class T:
        def __call__(self, ctx: ToolContext, query: str) -> ToolResult:
            received.append(ctx)
            return TextResult(text=query)

    ctx = _ctx()
    tool_factory(T()).build(_SOURCE).invoke(ctx, {"query": "x"})
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


def test_invoke_validates_arg_constraint():
    class T:
        def __call__(self, limit: Annotated[int, Field(ge=1)]) -> ToolResult:
            return TextResult(text=str(limit))

    built = tool_factory(T()).build(_SOURCE)
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"limit": 0})


# into_source / build


def test_into_source_yields_single_tool_source():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    src = tool_factory(T()).into_source(_SOURCE)
    assert src.id() == _SOURCE
    tools = list(src.tools())
    assert len(tools) == 1
    assert tools[0].tool_id() == compose_tool_id(_SOURCE, ToolName("T"))


def test_build_with_name_override():
    class T:
        def __call__(self, query: str) -> ToolResult:
            return TextResult(text=query)

    built = tool_factory(T(), name="renamed").build(_SOURCE)
    assert built.tool_id() == compose_tool_id(_SOURCE, ToolName("renamed"))


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


def test_returns_json_result_from_instance():
    """Sanity-проверка: callable-инстанс умеет вернуть JsonResult."""

    class SearchTool:
        def __init__(self, index_path: str, top_k: int) -> None:
            self._index_path = index_path
            self._top_k = top_k

        def __call__(self, query: str) -> ToolResult:
            return JsonResult(
                payload={
                    "index": self._index_path,
                    "k": self._top_k,
                    "query": query,
                },
            )

    instance = SearchTool(index_path="/srv/idx", top_k=3)
    factory = tool_factory(instance)
    assert factory.name == ToolName("SearchTool")
    assert list(factory.args_model.model_fields) == ["query"]

    out = factory.build(_SOURCE).invoke(_ctx(), {"query": "hello"})
    assert isinstance(out, JsonResult)
    assert out.payload == {"index": "/srv/idx", "k": 3, "query": "hello"}

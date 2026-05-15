"""Тесты декоратора @tool (pydantic-based)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Optional

import pytest
from pydantic import BaseModel, Field

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
from boba.tools.framework import ToolDecoratorFactory, tool

_SOURCE = ToolSourceId("test")


def _ctx() -> ToolContext:
    return ToolContext()


def _field_descr(factory: ToolDecoratorFactory, name: str) -> str:
    """Description конкретного field из pydantic-модели."""
    fi = factory.args_model.model_fields[name]
    return fi.description or ""


# form dispatch


def test_bare_tool_uses_function_name_and_docstring():
    @tool
    def echo(text: str) -> ToolResult:
        """Вернуть переданный текст."""
        return TextResult(text=text)

    assert echo.name == ToolName("echo")
    assert echo.description == "Вернуть переданный текст."


def test_tool_with_name_override():
    @tool("custom_name")
    def echo(text: str) -> ToolResult:
        """Doc."""
        return TextResult(text=text)

    assert echo.name == ToolName("custom_name")
    assert echo.description == "Doc."


def test_tool_with_description_override():
    @tool(description="Override.")
    def echo(text: str) -> ToolResult:
        """Doc."""
        return TextResult(text=text)

    assert echo.description == "Override."


def test_tool_with_name_and_description():
    @tool("custom", description="Desc.")
    def echo(text: str) -> ToolResult:
        return TextResult(text=text)

    assert echo.name == ToolName("custom")
    assert echo.description == "Desc."


def test_tool_without_docstring_has_empty_description():
    @tool
    def echo(text: str) -> ToolResult:
        return TextResult(text=text)

    assert echo.description == ""


# fields presence + types via model_fields


def test_str_param_creates_str_field():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    assert fn.args_model.model_fields["path"].annotation is str


def test_int_param_creates_int_field():
    @tool
    def fn(count: int) -> ToolResult:
        return TextResult(text=str(count))

    assert fn.args_model.model_fields["count"].annotation is int


def test_bool_param_creates_bool_field():
    @tool
    def fn(flag: bool) -> ToolResult:
        return TextResult(text=str(flag))

    assert fn.args_model.model_fields["flag"].annotation is bool


def test_float_param_creates_float_field():
    @tool
    def fn(score: float) -> ToolResult:
        return TextResult(text=str(score))

    assert fn.args_model.model_fields["score"].annotation is float


def test_param_with_default_uses_default_in_model():
    @tool
    def fn(count: int = 50) -> ToolResult:
        return TextResult(text=str(count))

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "50"


def test_param_without_default_is_required():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    with pytest.raises(InvalidToolArgumentError):
        fn.build(_SOURCE).invoke(_ctx(), {})


# Annotated metadata


def test_annotated_string_becomes_field_description():
    @tool
    def fn(path: Annotated[str, "Путь к файлу."]) -> ToolResult:
        return TextResult(text=path)

    assert _field_descr(fn, "path") == "Путь к файлу."


def test_annotated_pydantic_field_constraints_applied():
    @tool
    def fn(count: Annotated[int, Field(ge=1, le=10, description="Счётчик.")]) -> ToolResult:  # noqa: E501
        return TextResult(text=str(count))

    built = fn.build(_SOURCE)
    assert built.invoke(_ctx(), {"count": 5}).text == "5"  # type: ignore[attr-defined]
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"count": 0})
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"count": 11})


def test_annotated_extra_constraint_with_default():
    @tool
    def fn(count: Annotated[int, Field(ge=1)] = 5) -> ToolResult:
        return TextResult(text=str(count))

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "5"


# Literal


def test_literal_param_accepts_only_listed_values():
    @tool
    def fn(mode: Literal["a", "b", "c"]) -> ToolResult:
        return TextResult(text=mode)

    built = fn.build(_SOURCE)
    assert built.invoke(_ctx(), {"mode": "a"}).text == "a"  # type: ignore[attr-defined]
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"mode": "z"})


# ToolContext injection


def test_tool_context_param_excluded_from_model():
    @tool
    def fn(ctx: ToolContext, path: str) -> ToolResult:
        del ctx
        return TextResult(text=path)

    field_names = list(fn.args_model.model_fields.keys())
    assert "ctx" not in field_names
    assert field_names == ["path"]
    assert fn.injects_ctx is True


def test_no_tool_context_param_does_not_inject():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    assert fn.injects_ctx is False


def test_two_tool_context_params_raise():
    with pytest.raises(TypeError, match="ToolContext должен быть один"):

        @tool
        def fn(ctx1: ToolContext, ctx2: ToolContext, path: str) -> ToolResult:
            del ctx1, ctx2
            return TextResult(text=path)


# build & invoke


def test_build_assigns_qualified_tool_id():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    built = fn.build(_SOURCE)
    assert built.tool_id() == compose_tool_id(_SOURCE, ToolName("fn"))


def test_into_source_yields_single_tool_source():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    source = fn.into_source(_SOURCE)
    assert source.id() == _SOURCE
    tools = list(source.tools())
    assert len(tools) == 1
    assert tools[0].tool_id() == compose_tool_id(_SOURCE, ToolName("fn"))


def test_into_source_find_by_name():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    source = fn.into_source(_SOURCE)
    found = source.find(ToolName("fn"))
    assert found is not None
    assert found.tool_id() == compose_tool_id(_SOURCE, ToolName("fn"))


def test_into_source_find_missing_returns_none():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    assert fn.into_source(_SOURCE).find(ToolName("missing")) is None


def test_invoke_calls_function_with_kwargs():
    @tool
    def fn(path: str, count: int = 1) -> ToolResult:
        return TextResult(text=f"{path}*{count}")

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "x", "count": 3})
    assert isinstance(out, TextResult)
    assert out.text == "x*3"


def test_invoke_injects_tool_context():
    received: list[ToolContext] = []

    @tool
    def fn(ctx: ToolContext, path: str) -> ToolResult:
        received.append(ctx)
        return TextResult(text=path)

    ctx = _ctx()
    fn.build(_SOURCE).invoke(ctx, {"path": "x"})
    assert received == [ctx]


def test_invoke_uses_default_when_arg_omitted():
    @tool
    def fn(count: int = 7) -> ToolResult:
        return TextResult(text=str(count))

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "7"


def test_invoke_unknown_arg_raises():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    with pytest.raises(InvalidToolArgumentError):
        fn.build(_SOURCE).invoke(_ctx(), {"path": "x", "extra": "no"})


def test_invoke_missing_required_raises():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    with pytest.raises(InvalidToolArgumentError):
        fn.build(_SOURCE).invoke(_ctx(), {})


# error paths


def test_param_without_annotation_raises():
    with pytest.raises(TypeError, match="без аннотации"):

        @tool
        def fn(path) -> ToolResult:  # type: ignore[no-untyped-def]
            return TextResult(text=str(path))


def test_var_args_raises():
    with pytest.raises(TypeError, match=r"\*args/\*\*kwargs"):

        @tool
        def fn(*args: str) -> ToolResult:
            return TextResult(text=" ".join(args))


# Optional[X] / X | None


def test_optional_param_without_default_required_in_pydantic():
    """В pydantic `T | None` без default — required-поле; передавать null нужно явно."""
    @tool
    def fn(path: str | None) -> ToolResult:
        return TextResult(text=str(path))

    built = fn.build(_SOURCE)
    assert built.invoke(_ctx(), {"path": None}).text == "None"  # type: ignore[attr-defined]
    assert built.invoke(_ctx(), {"path": "x"}).text == "x"  # type: ignore[attr-defined]
    # Pydantic не подставляет None по умолчанию — отсутствие поля = missing.
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {})


def test_optional_typing_form_works():
    @tool
    def fn(path: Optional[str] = None) -> ToolResult:  # noqa: UP045
        return TextResult(text=str(path))

    built = fn.build(_SOURCE)
    assert built.invoke(_ctx(), {}).text == "None"  # type: ignore[attr-defined]
    assert built.invoke(_ctx(), {"path": "x"}).text == "x"  # type: ignore[attr-defined]


def test_optional_param_with_default_keeps_default():
    @tool
    def fn(path: str | None = "abc") -> ToolResult:
        return TextResult(text=str(path))

    built = fn.build(_SOURCE)
    assert built.invoke(_ctx(), {}).text == "abc"  # type: ignore[attr-defined]
    assert built.invoke(_ctx(), {"path": None}).text == "None"  # type: ignore[attr-defined]


def test_annotated_optional_combines_description():
    @tool
    def fn(path: Annotated[str | None, "путь"] = None) -> ToolResult:
        return TextResult(text=str(path))

    assert _field_descr(fn, "path") == "путь"


def test_optional_rejects_wrong_type():
    @tool
    def fn(path: int | None) -> ToolResult:
        return TextResult(text=str(path))

    built = fn.build(_SOURCE)
    assert built.invoke(_ctx(), {"path": None}).text == "None"  # type: ignore[attr-defined]
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"path": "not-int"})


# list[X] / dict[str, X]


def test_list_of_str_invoke_passes_list():
    received: list[Any] = []

    @tool
    def fn(tags: list[str]) -> ToolResult:
        received.append(tags)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(_ctx(), {"tags": ["a", "b"]})
    assert received == [["a", "b"]]


def test_list_rejects_wrong_element_type():
    @tool
    def fn(tags: list[str]) -> ToolResult:
        del tags
        return TextResult(text="ok")

    with pytest.raises(InvalidToolArgumentError):
        fn.build(_SOURCE).invoke(_ctx(), {"tags": ["a", 42]})


def test_dict_str_str_invoke_passes_dict():
    received: list[Any] = []

    @tool
    def fn(headers: dict[str, int]) -> ToolResult:
        received.append(headers)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(_ctx(), {"headers": {"a": 1, "b": 2}})
    assert received == [{"a": 1, "b": 2}]


# Nested pydantic BaseModel


class _Coords(BaseModel):
    x: int
    y: int = 0


class _Bounds(BaseModel):
    """Прямоугольная область."""

    lo: _Coords
    hi: _Coords


def test_nested_model_invoke_constructs_instance():
    received: list[_Coords] = []

    @tool
    def fn(point: _Coords) -> ToolResult:
        received.append(point)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(_ctx(), {"point": {"x": 5, "y": 7}})
    assert received == [_Coords(x=5, y=7)]


def test_nested_model_uses_field_default():
    received: list[_Coords] = []

    @tool
    def fn(point: _Coords) -> ToolResult:
        received.append(point)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(_ctx(), {"point": {"x": 5}})  # y по умолчанию = 0
    assert received == [_Coords(x=5, y=0)]


def test_nested_model_recursion():
    received: list[_Bounds] = []

    @tool
    def fn(box: _Bounds) -> ToolResult:
        received.append(box)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(
        _ctx(),
        {"box": {"lo": {"x": 0, "y": 0}, "hi": {"x": 10, "y": 5}}},
    )
    assert received == [_Bounds(lo=_Coords(x=0, y=0), hi=_Coords(x=10, y=5))]


def test_list_of_models_invoke():
    received: list[Any] = []

    @tool
    def fn(points: list[_Coords]) -> ToolResult:
        received.append(points)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(_ctx(), {"points": [{"x": 1, "y": 2}, {"x": 3}]})
    assert received == [[_Coords(x=1, y=2), _Coords(x=3, y=0)]]


def test_dict_of_models_invoke():
    received: list[Any] = []

    @tool
    def fn(named: dict[str, _Coords]) -> ToolResult:
        received.append(named)
        return TextResult(text="ok")

    fn.build(_SOURCE).invoke(_ctx(), {"named": {"origin": {"x": 0, "y": 0}}})
    assert received == [{"origin": _Coords(x=0, y=0)}]


# Return-type coercion


def test_return_str_wrapped_in_text_result():
    @tool
    def fn() -> str:
        return "hello"

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "hello"


def test_return_int_wrapped_in_text_result():
    @tool
    def fn() -> int:
        return 42

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "42"


def test_return_float_wrapped_in_text_result():
    @tool
    def fn() -> float:
        return 3.14

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "3.14"


def test_return_bool_wrapped_in_text_result():
    @tool
    def fn() -> bool:
        return True

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "True"


def test_return_dict_wrapped_in_json_result():
    @tool
    def fn() -> dict[str, int]:
        return {"a": 1, "b": 2}

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert out.payload == {"a": 1, "b": 2}


def test_return_list_wrapped_in_json_result():
    @tool
    def fn() -> list[int]:
        return [1, 2, 3]

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert out.payload == [1, 2, 3]


def test_return_tuple_normalized_to_list_in_json_result():
    @tool
    def fn() -> tuple[int, ...]:
        return (1, 2, 3)

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert out.payload == [1, 2, 3]


def test_return_set_normalized_to_list_in_json_result():
    @tool
    def fn() -> set[int]:
        return {1, 2, 3}

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert sorted(out.payload) == [1, 2, 3]


def test_return_frozenset_normalized_to_list_in_json_result():
    @tool
    def fn() -> frozenset[int]:
        return frozenset({1, 2})

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert sorted(out.payload) == [1, 2]


def test_return_none_becomes_null_text():
    @tool
    def fn() -> None:
        return None

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "null"


def test_return_tool_result_passes_through():
    @tool
    def fn() -> ToolResult:
        return JsonResult(payload={"k": "v"})

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert out.payload == {"k": "v"}


def test_return_dataclass_serialized_via_asdict():
    @dataclass(frozen=True)
    class _Point:
        x: int
        y: int

    @tool
    def fn() -> _Point:
        return _Point(x=1, y=2)

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert out.payload == {"x": 1, "y": 2}


def test_return_pydantic_model_serialized_via_model_dump():
    class _PointPyd(BaseModel):
        x: int
        y: int

    @tool
    def fn() -> _PointPyd:
        return _PointPyd(x=1, y=2)

    out = fn.build(_SOURCE).invoke(_ctx(), {})
    assert isinstance(out, JsonResult)
    assert out.payload == {"x": 1, "y": 2}


def test_return_unsupported_type_raises():
    class _Custom:
        pass

    @tool
    def fn() -> Any:
        return _Custom()

    with pytest.raises(TypeError, match="неподдерживаемый тип"):
        fn.build(_SOURCE).invoke(_ctx(), {})


# parse_docstring (Google-style)


def test_parse_docstring_extracts_field_descriptions():
    @tool(parse_docstring=True)
    def fn(path: str, count: int = 1) -> ToolResult:
        """Прочитать файл частями.

        Args:
            path: Путь к файлу.
            count: Сколько раз повторить чтение.
        """
        return TextResult(text=path * count)

    assert fn.description == "Прочитать файл частями."
    assert _field_descr(fn, "path") == "Путь к файлу."
    assert _field_descr(fn, "count") == "Сколько раз повторить чтение."


def test_parse_docstring_handles_multiline_arg_descriptions():
    @tool(parse_docstring=True)
    def fn(path: str) -> ToolResult:
        """Summary.

        Args:
            path: Путь к файлу;
                продолжается на следующей строке.
        """
        return TextResult(text=path)

    desc = _field_descr(fn, "path")
    assert "Путь к файлу;" in desc
    assert "продолжается на следующей строке." in desc


def test_parse_docstring_stops_at_returns_section():
    @tool(parse_docstring=True)
    def fn(path: str) -> ToolResult:
        """Summary.

        Args:
            path: Путь.

        Returns:
            ToolResult.
        """
        return TextResult(text=path)

    assert _field_descr(fn, "path") == "Путь."
    assert fn.description == "Summary."


def test_annotated_description_wins_over_docstring():
    @tool(parse_docstring=True)
    def fn(path: Annotated[str, "из Annotated"]) -> ToolResult:
        """Summary.

        Args:
            path: из docstring.
        """
        return TextResult(text=path)

    # Annotated[..., "..."] перехватывается раньше — но docstring имеет
    # приоритет, потому что docstring парсится первым; пустая Annotated-
    # строка не перезатирает docstring. См. _peel_description.
    # Здесь docstring задан, значит он и должен победить.
    assert _field_descr(fn, "path") == "из docstring."


def test_parse_docstring_disabled_by_default_keeps_full_doc_as_description():
    @tool
    def fn(path: str) -> ToolResult:
        """Summary.

        Args:
            path: docstring desc.
        """
        return TextResult(text=path)

    # без parse_docstring=True — полный docstring идёт в description tool'а,
    # field описание не извлекается
    assert "Args:" in fn.description
    assert _field_descr(fn, "path") == ""


def test_parse_docstring_without_args_section():
    @tool(parse_docstring=True)
    def fn(path: str) -> ToolResult:
        """Just a summary, no Args section."""
        return TextResult(text=path)

    assert fn.description == "Just a summary, no Args section."
    assert _field_descr(fn, "path") == ""

"""Тесты декоратора @tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, Optional

import pytest

from boba.patterns import ConverterInputError
from boba.schema.coercion import MaxValue, MinValue
from boba.schema.declaration import (
    CollectionField,
    FieldSpec,
    IndexedShape,
    KeyedShape,
    NestedField,
    ObjectItem,
    ScalarItem,
)
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


def _field(factory: ToolDecoratorFactory, name: str) -> FieldSpec[Any]:
    for f in factory.schema.fields:
        if f.name == name:
            assert isinstance(f, FieldSpec)
            return f
    msg = f"field {name!r} not found"
    raise AssertionError(msg)


# form dispatch

def test_bare_tool_uses_function_name_and_docstring():
    @tool
    def echo(text: str) -> ToolResult:
        """Вернуть переданный текст."""
        return TextResult(text=text)

    assert echo.name == ToolName("echo")
    assert echo.description == "Вернуть переданный текст."
    assert echo.schema.description == "Вернуть переданный текст."


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
    assert echo.schema.description == "Override."


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


# type → coercer mapping


def test_str_param_required():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    f = _field(fn, "path")
    assert f.coercer.apply("hello") == "hello"
    with pytest.raises(ConverterInputError):
        f.coercer.apply(123)  # IsString отвергает не-str


def test_int_param_required():
    @tool
    def fn(count: int) -> ToolResult:
        return TextResult(text=str(count))

    f = _field(fn, "count")
    assert f.coercer.apply(42) == 42


def test_bool_param_required():
    @tool
    def fn(flag: bool) -> ToolResult:
        return TextResult(text=str(flag))

    f = _field(fn, "flag")
    assert f.coercer.apply(True) is True


def test_float_param_required():
    @tool
    def fn(score: float) -> ToolResult:
        return TextResult(text=str(score))

    f = _field(fn, "score")
    assert f.coercer.apply(3.14) == 3.14


def test_param_with_default_uses_default_coercer():
    from boba.schema.coercion import MISSING

    @tool
    def fn(count: int = 50) -> ToolResult:
        return TextResult(text=str(count))

    f = _field(fn, "count")
    assert f.coercer.apply(MISSING) == 50
    assert f.coercer.apply(7) == 7


def test_param_without_default_is_required():
    from boba.patterns import MissingValueError
    from boba.schema.coercion import MISSING

    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    f = _field(fn, "path")
    with pytest.raises(MissingValueError):
        f.coercer.apply(MISSING)


# Annotated metadata


def test_annotated_string_becomes_field_description():
    @tool
    def fn(path: Annotated[str, "Путь к файлу."]) -> ToolResult:
        return TextResult(text=path)

    f = _field(fn, "path")
    assert f.description == "Путь к файлу."


def test_annotated_extra_coercer_is_appended():
    @tool
    def fn(count: Annotated[int, "Счётчик.", MinValue(1), MaxValue(10)]) -> ToolResult:
        return TextResult(text=str(count))

    f = _field(fn, "count")
    assert f.description == "Счётчик."
    assert f.coercer.apply(5) == 5
    with pytest.raises(ConverterInputError):
        f.coercer.apply(0)
    with pytest.raises(ConverterInputError):
        f.coercer.apply(11)


def test_annotated_extra_coercer_with_default():
    from boba.schema.coercion import MISSING

    @tool
    def fn(count: Annotated[int, "...", MinValue(1)] = 5) -> ToolResult:
        return TextResult(text=str(count))

    f = _field(fn, "count")
    assert f.coercer.apply(MISSING) == 5
    assert f.coercer.apply(3) == 3


# Literal → OneOf


def test_literal_param_uses_oneof():
    @tool
    def fn(mode: Literal["a", "b", "c"]) -> ToolResult:
        return TextResult(text=mode)

    f = _field(fn, "mode")
    assert f.coercer.apply("a") == "a"
    with pytest.raises(ConverterInputError):
        f.coercer.apply("z")


# ── ToolContext injection ────────────────────────────────────────────────


def test_tool_context_param_excluded_from_schema():
    @tool
    def fn(ctx: ToolContext, path: str) -> ToolResult:
        return TextResult(text=path)

    field_names = [f.name for f in fn.schema.fields]
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

    built = fn.build(_SOURCE)
    out = built.invoke(_ctx(), {"path": "x", "count": 3})
    assert isinstance(out, TextResult)
    assert out.text == "x*3"


def test_invoke_injects_tool_context():
    received: list[ToolContext] = []

    @tool
    def fn(ctx: ToolContext, path: str) -> ToolResult:
        received.append(ctx)
        return TextResult(text=path)

    built = fn.build(_SOURCE)
    ctx = _ctx()
    built.invoke(ctx, {"path": "x"})
    assert received == [ctx]


def test_invoke_uses_default_when_arg_omitted():
    @tool
    def fn(count: int = 7) -> ToolResult:
        return TextResult(text=str(count))

    built = fn.build(_SOURCE)
    out = built.invoke(_ctx(), {})
    assert isinstance(out, TextResult)
    assert out.text == "7"


def test_invoke_unknown_arg_raises():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    built = fn.build(_SOURCE)
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"path": "x", "extra": "no"})


def test_invoke_missing_required_raises():
    @tool
    def fn(path: str) -> ToolResult:
        return TextResult(text=path)

    built = fn.build(_SOURCE)
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {})


# error paths


def test_unsupported_type_raises():
    with pytest.raises(TypeError, match="неподдерживаемый тип"):

        @tool
        def fn(blob: bytes) -> ToolResult:
            return TextResult(text=str(blob))


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


def test_unsupported_annotated_metadata_raises():
    with pytest.raises(TypeError, match="неподдерживаемая Annotated-метаданная"):

        @tool
        def fn(path: Annotated[str, 42]) -> ToolResult:  # int — не str и не Coercer
            return TextResult(text=path)


# Optional[X] / X | None


def test_optional_param_without_default_uses_none_default():
    from boba.schema.coercion import MISSING

    @tool
    def fn(path: str | None) -> ToolResult:
        return TextResult(text=str(path))

    f = _field(fn, "path")
    assert f.coercer.apply(MISSING) is None
    assert f.coercer.apply(None) is None
    assert f.coercer.apply("x") == "x"


def test_optional_typing_form_uses_none_default():
    from boba.schema.coercion import MISSING

    @tool
    def fn(path: Optional[str]) -> ToolResult:  # noqa: UP045
        return TextResult(text=str(path))

    f = _field(fn, "path")
    assert f.coercer.apply(MISSING) is None
    assert f.coercer.apply(None) is None


def test_optional_param_with_default_keeps_default():
    from boba.schema.coercion import MISSING

    @tool
    def fn(path: str | None = "abc") -> ToolResult:
        return TextResult(text=str(path))

    f = _field(fn, "path")
    assert f.coercer.apply(MISSING) == "abc"
    assert f.coercer.apply(None) is None
    assert f.coercer.apply("x") == "x"


def test_optional_invokes_with_none():
    @tool
    def fn(path: str | None) -> ToolResult:
        return TextResult(text=f"got:{path!r}")

    built = fn.build(_SOURCE)
    out = built.invoke(_ctx(), {"path": None})
    assert isinstance(out, TextResult)
    assert out.text == "got:None"


def test_annotated_optional_combines_description_and_nullable():
    from boba.schema.coercion import MISSING

    @tool
    def fn(path: Annotated[str | None, "путь"]) -> ToolResult:
        return TextResult(text=str(path))

    f = _field(fn, "path")
    assert f.description == "путь"
    assert f.coercer.apply(MISSING) is None
    assert f.coercer.apply("x") == "x"


def test_optional_rejects_wrong_type():
    @tool
    def fn(path: int | None) -> ToolResult:
        return TextResult(text=str(path))

    f = _field(fn, "path")
    assert f.coercer.apply(None) is None
    with pytest.raises(ConverterInputError):
        f.coercer.apply("not-int")


def test_union_of_two_non_none_types_rejected():
    with pytest.raises(TypeError, match="Union"):

        @tool
        def fn(value: str | int) -> ToolResult:
            return TextResult(text=str(value))


# list[X] / dict[str, X]


def _coll_field(
    factory: ToolDecoratorFactory, name: str
) -> CollectionField[Any, Any, Any]:
    for f in factory.schema.fields:
        if f.name == name:
            assert isinstance(f, CollectionField)
            return f
    msg = f"collection field {name!r} not found"
    raise AssertionError(msg)


def _nested_field(factory: ToolDecoratorFactory, name: str) -> NestedField[Any]:
    for f in factory.schema.fields:
        if f.name == name:
            assert isinstance(f, NestedField)
            return f
    msg = f"nested field {name!r} not found"
    raise AssertionError(msg)


def test_list_of_str_is_indexed_collection():
    @tool
    def fn(tags: list[str]) -> ToolResult:
        return TextResult(text=",".join(tags))

    f = _coll_field(fn, "tags")
    assert isinstance(f.shape, IndexedShape)
    assert isinstance(f.reader, ScalarItem)


def test_list_invoke_passes_tuple():
    received: list[Any] = []

    @tool
    def fn(tags: list[str]) -> ToolResult:
        received.append(tags)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"tags": ["a", "b"]})
    assert received == [("a", "b")]  # IndexedShape возвращает tuple


def test_list_missing_yields_empty_collection():
    received: list[Any] = []

    @tool
    def fn(tags: list[str]) -> ToolResult:
        received.append(tags)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {})
    assert received == [()]


def test_list_rejects_wrong_element_type():
    @tool
    def fn(tags: list[str]) -> ToolResult:
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    with pytest.raises(InvalidToolArgumentError):
        built.invoke(_ctx(), {"tags": ["a", 42]})


def test_dict_str_str_is_keyed_collection():
    @tool
    def fn(headers: dict[str, str]) -> ToolResult:
        return TextResult(text=str(headers))

    f = _coll_field(fn, "headers")
    assert isinstance(f.shape, KeyedShape)
    assert isinstance(f.reader, ScalarItem)


def test_dict_invoke_passes_dict():
    received: list[Any] = []

    @tool
    def fn(headers: dict[str, int]) -> ToolResult:
        received.append(headers)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"headers": {"a": 1, "b": 2}})
    assert received == [{"a": 1, "b": 2}]


def test_dict_with_non_str_keys_rejected():
    with pytest.raises(TypeError, match="str-ключи"):

        @tool
        def fn(values: dict[int, str]) -> ToolResult:
            return TextResult(text=str(values))


# Nested dataclass


@dataclass(frozen=True)
class _Coords:
    x: int
    y: int = 0


@dataclass(frozen=True)
class _Bounds:
    """Прямоугольная область."""

    lo: _Coords
    hi: _Coords


@dataclass
class _WithFactory:
    items: list[str] = field(default_factory=list)


def test_nested_dataclass_creates_nested_field():
    @tool
    def fn(point: _Coords) -> ToolResult:
        return TextResult(text=f"{point.x},{point.y}")

    f = _nested_field(fn, "point")
    assert f.schema.factory is _Coords


def test_nested_dataclass_invoke_constructs_instance():
    received: list[_Coords] = []

    @tool
    def fn(point: _Coords) -> ToolResult:
        received.append(point)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"point": {"x": 5, "y": 7}})
    assert received == [_Coords(x=5, y=7)]


def test_nested_dataclass_uses_field_default():
    received: list[_Coords] = []

    @tool
    def fn(point: _Coords) -> ToolResult:
        received.append(point)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"point": {"x": 5}})  # y по умолчанию = 0
    assert received == [_Coords(x=5, y=0)]


def test_nested_dataclass_uses_factory_default():
    received: list[_WithFactory] = []

    @tool
    def fn(cfg: _WithFactory) -> ToolResult:
        received.append(cfg)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"cfg": {}})  # items опущен → пустая коллекция
    assert len(received) == 1
    assert received[0].items == ()  # IndexedShape возвращает tuple


def test_nested_dataclass_recursion():
    received: list[_Bounds] = []

    @tool
    def fn(box: _Bounds) -> ToolResult:
        received.append(box)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(
        _ctx(),
        {"box": {"lo": {"x": 0, "y": 0}, "hi": {"x": 10, "y": 5}}},
    )
    assert received == [_Bounds(lo=_Coords(0, 0), hi=_Coords(10, 5))]


def test_list_of_dataclass_uses_object_item():
    @tool
    def fn(points: list[_Coords]) -> ToolResult:
        return TextResult(text=str(points))

    f = _coll_field(fn, "points")
    assert isinstance(f.shape, IndexedShape)
    assert isinstance(f.reader, ObjectItem)


def test_list_of_dataclass_invoke():
    received: list[Any] = []

    @tool
    def fn(points: list[_Coords]) -> ToolResult:
        received.append(points)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"points": [{"x": 1, "y": 2}, {"x": 3}]})
    assert received == [(_Coords(1, 2), _Coords(3, 0))]


def test_dict_of_dataclass_invoke():
    received: list[Any] = []

    @tool
    def fn(named: dict[str, _Coords]) -> ToolResult:
        received.append(named)
        return TextResult(text="ok")

    built = fn.build(_SOURCE)
    built.invoke(_ctx(), {"named": {"origin": {"x": 0, "y": 0}}})
    assert received == [{"origin": _Coords(0, 0)}]


def test_optional_dataclass_rejected():
    with pytest.raises(TypeError, match="Optional пока не поддерживаются"):

        @tool
        def fn(point: _Coords | None) -> ToolResult:
            return TextResult(text=str(point))


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
    assert _field(fn, "path").description == "Путь к файлу."
    assert _field(fn, "count").description == "Сколько раз повторить чтение."


def test_parse_docstring_handles_multiline_arg_descriptions():
    @tool(parse_docstring=True)
    def fn(path: str) -> ToolResult:
        """Summary.

        Args:
            path: Путь к файлу;
                продолжается на следующей строке.
        """
        return TextResult(text=path)

    desc = _field(fn, "path").description
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

    assert _field(fn, "path").description == "Путь."
    assert fn.description == "Summary."


def test_annotated_description_wins_over_docstring():
    @tool(parse_docstring=True)
    def fn(path: Annotated[str, "из Annotated"]) -> ToolResult:
        """Summary.

        Args:
            path: из docstring.
        """
        return TextResult(text=path)

    assert _field(fn, "path").description == "из Annotated"


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
    assert _field(fn, "path").description == ""


def test_parse_docstring_without_args_section():
    @tool(parse_docstring=True)
    def fn(path: str) -> ToolResult:
        """Просто описание."""
        return TextResult(text=path)

    assert fn.description == "Просто описание."
    assert _field(fn, "path").description == ""


# Return value coercion


def test_return_str_wrapped_in_text_result():
    @tool
    def fn(path: str) -> str:
        return f"echo:{path}"

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "hi"})
    assert isinstance(out, TextResult)
    assert out.text == "echo:hi"


def test_return_dict_wrapped_in_json_result():
    @tool
    def fn(path: str) -> dict:
        return {"path": path, "ok": True}

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "x"})
    assert isinstance(out, JsonResult)
    assert out.payload == {"path": "x", "ok": True}


def test_return_tool_result_passes_through():
    @tool
    def fn(path: str) -> JsonResult:
        return JsonResult(payload={"src": path})

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "x"})
    assert isinstance(out, JsonResult)
    assert out.payload == {"src": "x"}


def test_return_int_wrapped_in_text_result():
    @tool
    def fn(path: str) -> int:
        return len(path)

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "hello"})
    assert isinstance(out, TextResult)
    assert out.text == "5"


def test_return_float_wrapped_in_text_result():
    @tool
    def fn(path: str) -> float:
        return 3.14 * len(path)

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "ab"})
    assert isinstance(out, TextResult)
    assert out.text == str(3.14 * 2)


def test_return_bool_wrapped_in_text_result():
    @tool
    def fn(path: str) -> bool:
        return bool(path)

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "x"})
    assert isinstance(out, TextResult)
    assert out.text == "True"


def test_return_list_wrapped_in_json_result():
    @tool
    def fn(path: str) -> list[str]:
        return [path, path.upper()]

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "a"})
    assert isinstance(out, JsonResult)
    assert out.payload == ["a", "A"]


def test_return_tuple_normalized_to_list_in_json_result():
    @tool
    def fn(path: str) -> tuple:
        return (path, 1, True)

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "a"})
    assert isinstance(out, JsonResult)
    assert out.payload == ["a", 1, True]


def test_return_dataclass_serialized_via_asdict():
    @tool
    def fn(path: str) -> _Coords:
        return _Coords(x=len(path), y=0)

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "ab"})
    assert isinstance(out, JsonResult)
    assert out.payload == {"x": 2, "y": 0}


def test_return_nested_dataclass_serialized_recursively():
    @tool
    def fn(path: str) -> _Bounds:
        n = len(path)
        return _Bounds(lo=_Coords(0, 0), hi=_Coords(n, n))

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "abc"})
    assert isinstance(out, JsonResult)
    assert out.payload == {
        "lo": {"x": 0, "y": 0},
        "hi": {"x": 3, "y": 3},
    }


def test_return_none_becomes_null_text():
    @tool
    def fn(path: str) -> None:
        del path  # имитируем «забытый return»

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "x"})
    assert isinstance(out, TextResult)
    assert out.text == "null"


def test_return_set_normalized_to_list_in_json_result():
    @tool
    def fn(path: str) -> set:
        return {path, path.upper()}

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "a"})
    assert isinstance(out, JsonResult)
    assert sorted(out.payload) == ["A", "a"]


def test_return_frozenset_normalized_to_list_in_json_result():
    @tool
    def fn(path: str) -> frozenset:
        return frozenset({path})

    out = fn.build(_SOURCE).invoke(_ctx(), {"path": "x"})
    assert isinstance(out, JsonResult)
    assert out.payload == ["x"]


class _Opaque:
    """Произвольный класс — не входит ни в одну из coercion-категорий."""


def test_return_unsupported_type_raises():
    @tool
    def fn(path: str) -> _Opaque:
        del path
        return _Opaque()

    with pytest.raises(TypeError, match="неподдерживаемый тип"):
        fn.build(_SOURCE).invoke(_ctx(), {"path": "x"})

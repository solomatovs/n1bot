"""Тесты `schema_from_callable`: callable → CallableSchema.

Покрывает универсальный API без зависимости от tools-специфики: разбор
функций и callable-инстансов, ignore_types, parse_docstring, имя и
описание, ошибки на некорректных подписях.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from boba.coercion import MISSING, MinValue
from boba.declaration import FieldSpec
from boba.schema import CallableSchema, schema_from_callable


def _field(schema: Any, name: str) -> FieldSpec[Any]:
    for f in schema.fields:
        if f.name == name:
            assert isinstance(f, FieldSpec)
            return f
    msg = f"field {name!r} not found"
    raise AssertionError(msg)


# ── Имя / описание ────────────────────────────────────────────────────────


def test_function_name_and_docstring():
    def search(query: str) -> str:
        """Поиск в базе."""
        return query

    parsed = schema_from_callable(search)
    assert isinstance(parsed, CallableSchema)
    assert parsed.name == "search"
    assert parsed.description == "Поиск в базе."
    assert parsed.schema.description == "Поиск в базе."


def test_callable_instance_uses_class_name_and_docstring():
    class Search:
        """Поиск по индексу."""

        def __call__(self, query: str) -> str:
            return query

    parsed = schema_from_callable(Search())
    assert parsed.name == "Search"
    assert parsed.description == "Поиск по индексу."


def test_function_without_docstring_has_empty_description():
    def fn(q: str) -> str:
        return q

    assert schema_from_callable(fn).description == ""


# ── Параметры → schema ────────────────────────────────────────────────────


def test_required_and_default_params():
    def fn(query: str, limit: int = 10) -> str:
        return query

    parsed = schema_from_callable(fn)
    fields = {f.name: f for f in parsed.schema.fields}
    assert set(fields) == {"query", "limit"}
    assert fields["limit"].coercer.apply(MISSING) == 10


def test_annotated_description_and_extra_coercer():
    def fn(
        query: Annotated[str, "Поисковая строка."],
        limit: Annotated[int, "Лимит.", MinValue(1)] = 5,
    ) -> str:
        return query

    parsed = schema_from_callable(fn)
    assert _field(parsed.schema, "query").description == "Поисковая строка."
    f = _field(parsed.schema, "limit")
    assert f.description == "Лимит."
    assert f.coercer.apply(2) == 2


def test_optional_param_default_none():
    def fn(name: str | None = None) -> str:
        return name or ""

    f = _field(schema_from_callable(fn).schema, "name")
    assert f.coercer.apply(MISSING) is None


# ── ignore_types ──────────────────────────────────────────────────────────


class _Ctx:
    """Sentinel-тип контекста — пропускается через ignore_types."""


class _A:
    pass


class _B:
    pass


def test_ignore_types_skips_param_and_reports_injected():
    def fn(ctx: _Ctx, query: str) -> str:
        return query

    parsed = schema_from_callable(fn, ignore_types=(_Ctx,))
    assert parsed.injected == ("ctx",)
    assert [f.name for f in parsed.schema.fields] == ["query"]


def test_no_ignore_types_keeps_all_params():
    def fn(a: str, b: int) -> str:
        return a

    parsed = schema_from_callable(fn)
    assert parsed.injected == ()
    assert {f.name for f in parsed.schema.fields} == {"a", "b"}


def test_multiple_ignored_params_reported_in_order():
    def fn(a: _A, b: _B, q: str) -> str:
        return q

    parsed = schema_from_callable(fn, ignore_types=(_A, _B))
    assert parsed.injected == ("a", "b")
    assert [f.name for f in parsed.schema.fields] == ["q"]


# ── parse_docstring ───────────────────────────────────────────────────────


def test_parse_docstring_extracts_arg_descriptions():
    def fn(path: str, count: int = 1) -> str:
        """Считать файл частями.

        Args:
            path: Путь к файлу.
            count: Сколько раз повторить.
        """
        return path * count

    parsed = schema_from_callable(fn, parse_docstring=True)
    assert parsed.description == "Считать файл частями."
    assert _field(parsed.schema, "path").description == "Путь к файлу."
    assert _field(parsed.schema, "count").description == "Сколько раз повторить."


def test_annotated_description_wins_over_docstring():
    def fn(path: Annotated[str, "из Annotated"]) -> str:
        """Summary.

        Args:
            path: из docstring.
        """
        return path

    parsed = schema_from_callable(fn, parse_docstring=True)
    assert _field(parsed.schema, "path").description == "из Annotated"


def test_parse_docstring_disabled_keeps_full_doc_as_description():
    def fn(path: str) -> str:
        """Summary.

        Args:
            path: desc.
        """
        return path

    parsed = schema_from_callable(fn)
    assert "Args:" in parsed.description
    assert _field(parsed.schema, "path").description == ""


# ── Стейт callable-инстанса ───────────────────────────────────────────────


def test_callable_instance_self_not_in_schema():
    class T:
        def __call__(self, q: str) -> str:
            return q

    fields = schema_from_callable(T()).schema.fields
    assert [f.name for f in fields] == ["q"]


# ── Ошибки ────────────────────────────────────────────────────────────────


def test_class_itself_rejected():
    class T:
        def __call__(self, q: str) -> str:
            return q

    with pytest.raises(TypeError, match="ожидается функция или callable"):
        schema_from_callable(T)  # type: ignore[arg-type]


def test_non_callable_rejected():
    with pytest.raises(TypeError, match="ожидается функция или callable"):
        schema_from_callable(42)  # type: ignore[arg-type]


def test_var_args_rejected():
    def fn(*args: str) -> str:
        return " ".join(args)

    with pytest.raises(TypeError, match=r"\*args/\*\*kwargs"):
        schema_from_callable(fn)


def test_param_without_annotation_rejected():
    def fn(path) -> str:  # type: ignore[no-untyped-def]
        return str(path)

    with pytest.raises(TypeError, match="без аннотации"):
        schema_from_callable(fn)


def test_unsupported_type_rejected():
    def fn(blob: bytes) -> str:
        return str(blob)

    with pytest.raises(TypeError, match="неподдерживаемый тип"):
        schema_from_callable(fn)

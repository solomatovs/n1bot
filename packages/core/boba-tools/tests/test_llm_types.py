"""Тесты `LLMStringList` — coercion типового LLM-входа в `list[str]`."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from boba.tools import LLMStringList


class _Model(BaseModel):
    items: LLMStringList


class _Nullable(BaseModel):
    items: LLMStringList | None = None


def test_passthrough_list_of_str() -> None:
    assert _Model(items=["a", "b"]).items == ["a", "b"]


def test_json_array_string() -> None:
    assert _Model(items='["950276", "950278"]').items == ["950276", "950278"]


def test_json_array_with_int_elements_coerced_to_str() -> None:
    # LLM иногда передаёт числа в JSON-массиве — приводим к str.
    assert _Model(items="[950276, 950278]").items == ["950276", "950278"]


def test_json_array_string_with_whitespace_prefix() -> None:
    assert _Model(items='   ["a","b"]   ').items == ["a", "b"]


def test_single_string_becomes_singleton_list() -> None:
    assert _Model(items="950276").items == ["950276"]


def test_csv_string_split_with_strip() -> None:
    assert _Model(items="a, b ,c").items == ["a", "b", "c"]


def test_csv_string_empty_segments_dropped() -> None:
    assert _Model(items="a,,b,").items == ["a", "b"]


def test_invalid_json_array_falls_through_to_pydantic_error() -> None:
    # `[abc` начинается с `[`, json.loads падает, возвращаем v как есть —
    # дальше pydantic скажет, что это не list.
    with pytest.raises(ValidationError):
        _Model(items="[abc")


def test_non_array_json_object_string_treated_as_singleton() -> None:
    # Не начинается с `[`, идёт через CSV-fallback: одна строка → singleton.
    assert _Model(items='{"a": 1}').items == ['{"a": 1}']



def test_none_with_nullable_wrapper() -> None:
    assert _Nullable(items=None).items is None


def test_int_passthrough_then_pydantic_rejects() -> None:
    with pytest.raises(ValidationError):
        _Model(items=42)

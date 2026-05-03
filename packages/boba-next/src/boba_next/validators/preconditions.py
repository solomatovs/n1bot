"""Предусловия наличия значения: NotNull, Default, Nullable."""

from __future__ import annotations

from typing import Any

from boba.patterns import Converter, MissingValueError
from boba_next.validators.base import MISSING, ValueConverter

__all__ = ["Default", "NotNull", "Nullable"]


class NotNull(Converter[Any, Any]):
    """Item-level non-null: MISSING/None/NullValue → MissingValueError.

    Используется внутри `ScalarItem.converter` для коллекций — отвергает
    null-элементы массивов/словарей. Для root-level «поле обязательно в
    объекте» используй декларативный `FieldSpec(..., required=True)`.
    """

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            raise MissingValueError("значение отсутствует")
        if ValueConverter.unwrap(value) is None:
            raise MissingValueError("null недопустим")
        return value


class Default(Converter[Any, Any]):
    """Подставить default при MISSING."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            return self._value
        return value


class Nullable(Converter[Any, Any]):
    """MISSING/None/NullValue → None; иначе делегирует во внутренний."""

    def __init__(self, inner: Converter[Any, Any]) -> None:
        self._inner = inner

    def convert(self, value: Any) -> Any:
        if value is MISSING or ValueConverter.unwrap(value) is None:
            return None
        return self._inner.convert(value)

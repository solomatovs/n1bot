"""Предусловия наличия значения: NotNull, Default, Nullable."""

from __future__ import annotations

from typing import Any

from boba.coercion.base import MISSING, Coercer, ValueCoercer
from boba.patterns import MissingValueError

__all__ = ["Default", "NotNull", "Nullable"]


class NotNull(Coercer[Any, Any]):
    """Item-level non-null: MISSING/None/NullValue → MissingValueError.

    Используется внутри `ScalarItem.coercer` для коллекций — отвергает
    null-элементы массивов/словарей. Для root-level «поле обязательно в
    объекте» используй декларативный `FieldSpec(..., required=True)`.
    """

    def apply(self, value: Any) -> Any:
        if value is MISSING:
            raise MissingValueError("значение отсутствует")
        if ValueCoercer.unwrap(value) is None:
            raise MissingValueError("null недопустим")
        return value


class Default(Coercer[Any, Any]):
    """Подставить default при MISSING."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def apply(self, value: Any) -> Any:
        if value is MISSING:
            return self._value
        return value


class Nullable(Coercer[Any, Any]):
    """MISSING/None/NullValue → None; иначе делегирует во внутренний."""

    def __init__(self, inner: Coercer[Any, Any]) -> None:
        self._inner = inner

    def apply(self, value: Any) -> Any:
        if value is MISSING or ValueCoercer.unwrap(value) is None:
            return None
        return self._inner.apply(value)

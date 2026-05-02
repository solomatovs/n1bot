"""Предусловия наличия значения: Required, Default, Nullable."""

from __future__ import annotations

from typing import Any

from boba.patterns import Converter, MissingValueError
from boba_next.validators.base import MISSING, ValueConverter

__all__ = ["Default", "Nullable", "Required"]


class Required(Converter[Any, Any]):
    """Параметр обязателен; MISSING/None/NullValue → MissingValueError."""

    def convert(self, value: Any) -> Any:
        if value is MISSING:
            raise MissingValueError("параметр обязателен — значение не передано")
        if ValueConverter.unwrap(value) is None:
            raise MissingValueError("параметр обязателен — null недопустим")
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

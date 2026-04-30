"""Скалярные ограничения значения: OneOf, MinValue, MaxValue, MinLength, MaxLength, NonEmpty."""

from __future__ import annotations

from collections.abc import Sized
from typing import Any, ClassVar

from boba.domain.core.patterns import Converter, ConverterInputError

__all__ = [
    "MaxLength",
    "MaxValue",
    "MinLength",
    "MinValue",
    "NonEmpty",
    "OneOf",
]


class OneOf(Converter[Any, Any]):
    """Значение должно быть в фиксированном наборе."""

    _MIN_OPTIONS: ClassVar[int] = 1

    def __init__(self, *options: Any) -> None:
        if len(options) < self._MIN_OPTIONS:
            raise ValueError(f"OneOf требует минимум {self._MIN_OPTIONS} вариант")
        self._options = options

    def convert(self, value: Any) -> Any:
        if value not in self._options:
            raise ConverterInputError(
                f"должно быть одно из {list(self._options)}, получено {value!r}"
            )
        return value


class MinValue(Converter[Any, Any]):
    """Значение >= threshold."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if value < self._threshold:
            raise ConverterInputError(
                f"должно быть >= {self._threshold}, получено {value}"
            )
        return value


class MaxValue(Converter[Any, Any]):
    """Значение <= threshold."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if value > self._threshold:
            raise ConverterInputError(
                f"должно быть <= {self._threshold}, получено {value}"
            )
        return value


class MinLength(Converter[Any, Any]):
    """Длина >= threshold."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) < self._threshold:
            raise ConverterInputError(
                f"длина должна быть >= {self._threshold}, получено {len(value)}"
            )
        return value


class MaxLength(Converter[Any, Any]):
    """Длина <= threshold."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(
                f"длина не определена для {type(value).__name__}"
            )
        if len(value) > self._threshold:
            raise ConverterInputError(
                f"длина должна быть <= {self._threshold}, получено {len(value)}"
            )
        return value


class NonEmpty(Converter[Any, Any]):
    """Длина > 0."""

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError("длина не определена")
        if len(value) == 0:
            raise ConverterInputError("значение не должно быть пустым")
        return value

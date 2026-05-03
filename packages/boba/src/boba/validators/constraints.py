"""Ограничения значения: OneOf, MinValue, MaxValue, MinLength, MaxLength, NonEmpty.

Каждый реализует SchemaContributor — пишет соответствующее ограничение в prop.
"""

from __future__ import annotations

from collections.abc import Sized
from typing import Any, ClassVar

from boba.patterns import Converter, ConverterInputError
from boba.validators.base import SchemaContributor

__all__ = [
    "MaxLength",
    "MaxValue",
    "MinLength",
    "MinValue",
    "NonEmpty",
    "OneOf",
]


class OneOf(Converter[Any, Any], SchemaContributor):
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

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["enum"] = list(self._options)


class MinValue(Converter[Any, Any], SchemaContributor):
    """Значение >= threshold."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if value < self._threshold:
            raise ConverterInputError(
                f"должно быть >= {self._threshold}, получено {value}"
            )
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["minimum"] = self._threshold


class MaxValue(Converter[Any, Any], SchemaContributor):
    """Значение <= threshold."""

    def __init__(self, threshold: int | float) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if value > self._threshold:
            raise ConverterInputError(
                f"должно быть <= {self._threshold}, получено {value}"
            )
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["maximum"] = self._threshold


class MinLength(Converter[Any, Any], SchemaContributor):
    """Длина >= threshold."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(f"длина не определена для {type(value).__name__}")
        if len(value) < self._threshold:
            raise ConverterInputError(
                f"длина должна быть >= {self._threshold}, получено {len(value)}"
            )
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["minLength"] = self._threshold


class MaxLength(Converter[Any, Any], SchemaContributor):
    """Длина <= threshold."""

    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError(f"длина не определена для {type(value).__name__}")
        if len(value) > self._threshold:
            raise ConverterInputError(
                f"длина должна быть <= {self._threshold}, получено {len(value)}"
            )
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["maxLength"] = self._threshold


class NonEmpty(Converter[Any, Any], SchemaContributor):
    """Длина > 0."""

    def convert(self, value: Any) -> Any:
        if not isinstance(value, Sized):
            raise ConverterInputError("длина не определена")
        if len(value) == 0:
            raise ConverterInputError("значение не должно быть пустым")
        return value

    def contribute(self, prop: dict[str, Any]) -> None:
        prop["minLength"] = 1

"""Object-level invariants для ObjectSchema.invariants:
работают над собранным dict[str, Any], а не над отдельным значением.
"""

from __future__ import annotations

from typing import Any, ClassVar

from boba.coercion.base import Coercer
from boba.patterns import ConverterInputError

__all__ = ["MutuallyExclusive", "Ordered", "RequiresTogether"]


class MutuallyExclusive(Coercer[dict[str, Any], dict[str, Any]]):
    """Максимум один из перечисленных полей задан одновременно."""

    _MIN_NAMES: ClassVar[int] = 2

    def __init__(self, *names: str) -> None:
        if len(names) < self._MIN_NAMES:
            raise ValueError(
                f"MutuallyExclusive требует минимум {self._MIN_NAMES} имени"
            )
        self._names = names

    def apply(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if len(present) > 1:
            raise ConverterInputError(
                f"параметры {present} взаимоисключающие — задайте только один"
            )
        return value


class RequiresTogether(Coercer[dict[str, Any], dict[str, Any]]):
    """Перечисленные поля — либо все, либо ни одного."""

    _MIN_NAMES: ClassVar[int] = 2

    def __init__(self, *names: str) -> None:
        if len(names) < self._MIN_NAMES:
            raise ValueError(
                f"RequiresTogether требует минимум {self._MIN_NAMES} имени"
            )
        self._names = names

    def apply(self, value: dict[str, Any]) -> dict[str, Any]:
        present = [n for n in self._names if n in value]
        if present and len(present) != len(self._names):
            missing = [n for n in self._names if n not in value]
            raise ConverterInputError(
                f"параметры {list(self._names)} должны быть заданы вместе; "
                f"отсутствуют: {missing}"
            )
        return value


class Ordered(Coercer[dict[str, Any], dict[str, Any]]):
    """value[first] <= value[second], когда оба заданы."""

    def __init__(self, first: str, second: str) -> None:
        self._first = first
        self._second = second

    def apply(self, value: dict[str, Any]) -> dict[str, Any]:
        if (
            self._first in value
            and self._second in value
            and value[self._first] > value[self._second]
        ):
            raise ConverterInputError(
                f"{self._first}={value[self._first]!r} должно быть <= "
                f"{self._second}={value[self._second]!r}"
            )
        return value
